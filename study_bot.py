import os
import re
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import yt_dlp
import requests
import gdown
from markdown_pdf import MarkdownPdf, Section
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)

# Uses gemini-3.8-flash first, with automatic fallback if needed
MODEL_CANDIDATES = ["gemini-3.8-flash", "gemini-3-flash-preview", "gemini-2.5-flash"]

# Clean styling for the generated PDF notes (supports Unicode symbols & prevents right-edge cutoff)
PDF_CSS = """
body { font-family: Arial, Helvetica, sans-serif; line-height: 1.6; color: #1e293b; font-size: 10.5pt; word-wrap: break-word; }
h1 { color: #1e3a8a; border-bottom: 2px solid #1e3a8a; padding-bottom: 6px; margin-top: 0; font-size: 18pt; }
h2 { color: #1d4ed8; margin-top: 16px; border-bottom: 1px solid #cbd5e1; padding-bottom: 4px; font-size: 14pt; }
h3 { color: #2563eb; margin-top: 12px; font-size: 12pt; }
code, pre { background-color: #f1f5f9; padding: 2px 5px; border-radius: 4px; font-family: Courier, monospace; }
blockquote { border-left: 4px solid #3b82f6; padding-left: 12px; color: #475569; background: #f8fafc; }
ul, ol { margin-bottom: 10px; padding-left: 18px; }
li { margin-bottom: 6px; }
p { margin-bottom: 8px; }
"""


# --- HELPER FUNCTIONS ---

def call_gemini(contents):
    """Calls Gemini 3.8 Flash with automatic fallback."""
    last_err = None
    for model_name in MODEL_CANDIDATES:
        try:
            return client.models.generate_content(
                model=model_name,
                contents=contents,
            )
        except Exception as e:
            last_err = e
            if "404" in str(e) or "NOT_FOUND" in str(e):
                continue
            raise e
    raise last_err


def clean_latex_to_unicode(text: str) -> str:
    """Strips raw LaTeX ($...$, \\text{}, \\sigma, subscripts/superscripts) into clean Unicode."""
    # 1. Remove $$ and $ math delimiters
    text = re.sub(r'\$\$(.*?)\$\$', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\$(.*?)\$', r'\1', text)

    # 2. Strip \text{...}, \mathrm{...}, \mathbf{...} wrappers (run multiple times for nested tags)
    for _ in range(4):
        text = re.sub(r'\\(?:text|mathrm|mathbf|mathit|textbf|mathcal)\{([^}]*)\}', r'\1', text)

    # 3. Replace LaTeX Greek letters, arrows, and math symbols with clean Unicode
    latex_symbols = {
        r'\sigma^*': 'σ*', r'\pi^*': 'π*',
        r'\sigma': 'σ', r'\pi': 'π', r'\Delta': 'Δ', r'\delta': 'δ',
        r'\theta': 'θ', r'\lambda': 'λ', r'\mu': 'μ', r'\nu': 'ν',
        r'\alpha': 'α', r'\beta': 'β', r'\gamma': 'γ', r'\omega': 'ω',
        r'\rightarrow': '→', r'\to': '→', r'\leftarrow': '←',
        r'\leftrightarrow': '↔', r'\rightleftharpoons': '⇌', r'\Rightarrow': '⇒',
        r'\ge': '≥', r'\geq': '≥', r'\le': '≤', r'\leq': '≤',
        r'\ne': '≠', r'\neq': '≠', r'\approx': '≈', r'\equiv': '≡',
        r'\pm': '±', r'\times': '×', r'\cdot': '•', r'\circ': '°',
        r'\infty': '∞', r'\propto': '∝', r'\partial': '∂', r'\sqrt': '√',
    }
    for k, v in latex_symbols.items():
        text = text.replace(k, v)

    # 4. Convert Superscripts: ^{2+}, ^2, ^*
    sup_map = str.maketrans("0123456789+-=()nxyz*", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿˣʸᶻ*")
    text = re.sub(
        r'\^\{([^}]+)\}',
        lambda m: m.group(1).translate(sup_map),
        text
    )
    text = re.sub(
        r'\^([0-9+\-=*])',
        lambda m: m.group(1).translate(sup_map),
        text
    )

    # 5. Convert Subscripts: _{2p_x}, _{1s}, _2
    sub_map = str.maketrans("0123456789+-=()aeioruvxspyz", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑᵢₒᵣᵤᵥₓₛₚᵧ₂")
    text = re.sub(
        r'_\{([^}]+)\}',
        lambda m: m.group(1).replace('_', '').translate(sub_map),
        text
    )
    text = re.sub(
        r'_([0-9a-zA-Z])',
        lambda m: m.group(1).translate(sub_map),
        text
    )

    # 6. Turn Markdown checkboxes "- [ ]" into nice ballot boxes "☐"
    text = re.sub(r'^\s*[\*\-]\s*\[\s*\]\s*', '• ☐ ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[\*\-]\s*\[x\]\s*', '• ☑ ', text, flags=re.MULTILINE | re.IGNORECASE)

    # 7. Clean up leftover LaTeX spacing/braces
    text = text.replace(r'\,', ' ').replace(r'\;', ' ').replace(r'\{', '{').replace(r'\}', '}')
    return text


def format_for_telegram_chat(markdown_text: str) -> str:
    """Makes Markdown notes look clean in Telegram plain-text messages."""
    t = markdown_text
    t = re.sub(r'^###\s+(.*)', r'🔹 \1', t, flags=re.MULTILINE)
    t = re.sub(r'^##\s+(.*)', r'\n📌 \1', t, flags=re.MULTILINE)
    t = re.sub(r'^#\s+(.*)', r'📘 \1', t, flags=re.MULTILINE)
    t = t.replace('**', '')
    return t


def extract_video_id(url: str) -> str | None:
    """Extracts the 11-character YouTube video ID from any YouTube URL format."""
    parsed = urlparse(url)
    if parsed.hostname in ('youtu.be', 'www.youtu.be'):
        return parsed.path[1:].split('?')[0]
    if parsed.hostname in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
        if parsed.path == '/watch':
            return parse_qs(parsed.query).get('v', [None])[0]
        if parsed.path.startswith(('/embed/', '/v/', '/live/', '/shorts/')):
            return parsed.path.split('/')[2]
    return None


def get_video_metadata(url: str) -> dict:
    """Extracts video title and description using yt-dlp without downloading the video."""
    ydl_opts = {'quiet': True, 'skip_download': True, 'no_warnings': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            'title': info.get('title', 'Lecture_Notes'),
            'description': info.get('description', ''),
        }


def extract_resource_links(description: str) -> dict:
    """Finds PDFs, Google Drive/Docs links, and unshortens bit.ly/tinyurl links."""
    urls = re.findall(r'(https?://[^\s\)\]\>\"\'<]+)', description)
    drive_links, direct_pdfs, other_links = [], [], []
    ignore_domains = (
        'youtube.com', 'youtu.be', 'instagram.com', 'twitter.com',
        'x.com', 'facebook.com', 't.me', 'linkedin.com', 'quora.com', 'onelink.me'
    )
    shorteners = ('bit.ly', 'tinyurl.com', 'goo.gl', 'cutt.ly', 'rb.gy', 'is.gd')

    for url in set(urls):
        clean_url = url.rstrip('.,;!')
        lower_url = clean_url.lower()

        if any(domain in lower_url for domain in ignore_domains):
            continue

        # Unshorten bit.ly / tinyurl links to check if they point to Drive or a PDF
        if any(short in lower_url for short in shorteners):
            try:
                resp = requests.head(clean_url, allow_redirects=True, timeout=5)
                if resp.url:
                    clean_url = resp.url
                    lower_url = clean_url.lower()
            except Exception:
                pass

        if 'drive.google.com' in lower_url or 'docs.google.com' in lower_url:
            drive_links.append(clean_url)
        elif lower_url.endswith('.pdf'):
            direct_pdfs.append(clean_url)
        elif not any(domain in lower_url for domain in ignore_domains):
            other_links.append(clean_url)

    return {'drive': drive_links, 'pdfs': direct_pdfs, 'other': other_links}


def fetch_transcript(video_id: str) -> str:
    """Fetches the transcript (compatible with youtube-transcript-api v1.0+ and yt-dlp fallback)."""
    try:
        ytt_api = YouTubeTranscriptApi()
        if hasattr(ytt_api, 'fetch'):
            fetched = ytt_api.fetch(video_id, languages=['en', 'en-IN', 'hi'])
            return " ".join(
                snippet.text if hasattr(snippet, 'text') else snippet['text']
                for snippet in fetched
            )
        else:
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['en', 'en-IN', 'hi'])
            return " ".join([entry['text'] for entry in transcript_list])

    except Exception as primary_err:
        # Fallback: Extract auto-subtitles directly via yt-dlp
        try:
            url = f"https://www.youtube.com/watch?v={video_id}"
            ydl_opts = {'quiet': True, 'skip_download': True, 'no_warnings': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                subs = info.get('subtitles') or info.get('automatic_captions') or {}

                for lang in ['en', 'en-IN', 'hi']:
                    if lang in subs:
                        for fmt in subs[lang]:
                            if fmt.get('ext') == 'json3':
                                r = requests.get(fmt['url'], timeout=15)
                                data = r.json()
                                words = []
                                for event in data.get('events', []):
                                    for seg in event.get('segs', []):
                                        if 'utf8' in seg:
                                            words.append(seg['utf8'])
                                text = "".join(words).replace("\n", " ")
                                if text.strip():
                                    return text
        except Exception:
            pass
        raise RuntimeError(f"Could not fetch transcript: {primary_err}")


def generate_study_notes(title: str, transcript: str, description: str) -> str:
    """Uses Gemini 3.8 Flash to turn raw transcript into structured, Unicode-clean study notes."""
    prompt = f"""
    You are an expert academic note-taker. Create comprehensive, well-structured, 
    easy-to-read study notes from the following lecture video.
    
    Video Title: {title}
    
    Video Description (for context/topics):
    {description[:1500]}
    
    Lecture Transcript:
    {transcript}
    
    STRICT FORMATTING RULES:
    - DO NOT use LaTeX math mode ($...$ or $$...$$) or commands like \\text{{}}, \\sigma, \\rightarrow, \\ge.
    - Instead, ALWAYS use clean Unicode characters directly:
      • Subscripts/Superscripts: H₂O, CO₂, sp³d², x², 10⁻⁵
      • Symbols: σ, π, Δ, θ, λ, →, ⇌, ≥, ≤, ≠, ≈, ±, ×, °
      • Checkboxes: ☐
    
    Structure the notes in clean Markdown with:
    1. ## Core Summary / TL;DR (3-4 bullet points)
    2. ## Detailed Topic-by-Topic Breakdown (Use clear headings and bullet points)
    3. ## Key Formulas, Definitions, or Rules (Highlighted clearly for quick memorization)
    4. ## Important Examples / Exceptions / Edge Cases Discussed
    5. ## Quick Revision Checklist (Use ☐ checkboxes)
    
    Note: If the transcript is in Hinglish or Hindi, translate and write all the final notes cleanly in English.
    """
    response = call_gemini(prompt)
    return clean_latex_to_unicode(response.text)


def create_notes_pdf(title: str, markdown_notes: str, output_path: str) -> str:
    """Compiles the Markdown study notes into a formatted PDF with a Table of Contents."""
    pdf = MarkdownPdf(toc_level=2, optimize=True)
    full_markdown = f"# {title}\n\n{markdown_notes}"
    pdf.add_section(Section(full_markdown), user_css=PDF_CSS)
    pdf.meta["title"] = title
    pdf.save(output_path)
    return output_path


def answer_text_question(question: str, lecture_data: dict, chat_history: list) -> str:
    """Answers a follow-up text question using the lecture transcript and generated notes."""
    history_str = "\n".join([f"Q: {q}\nA: {a}" for q, a in chat_history[-4:]])
    prompt = f"""
    You are a helpful study assistant. Answer the student's question clearly and concisely 
    based on the lecture below. 
    IMPORTANT: Do NOT use LaTeX ($...$ or \\text{{}}). Use plain Unicode symbols (e.g., H₂O, sp³, σ, →, ≥) so it is easy to read on Telegram.

    Lecture Title: {lecture_data['title']}
    
    Generated Notes:
    {lecture_data['notes']}

    Full Lecture Transcript (reference):
    {lecture_data['transcript'][:60000]}

    Recent Q&A History:
    {history_str}

    Student's Question: {question}
    """
    response = call_gemini(prompt)
    return clean_latex_to_unicode(response.text)


def answer_voice_question(audio_bytes: bytes, lecture_data: dict) -> str:
    """Answers a voice note question directly using Gemini's multimodal audio support."""
    prompt = f"""
    Listen to the student's voice note question and answer it clearly and concisely based on 
    this lecture. First write a 1-line transcription of what she asked (e.g., '🎤 You asked: ...'), 
    and then provide the clear explanation.
    IMPORTANT: Do NOT use LaTeX ($...$ or \\text{{}}). Use plain Unicode symbols (e.g., H₂O, sp³, σ, →, ≥).

    Lecture Title: {lecture_data['title']}
    Generated Notes:
    {lecture_data['notes']}
    Full Lecture Transcript:
    {lecture_data['transcript'][:60000]}
    """
    response = call_gemini([
        types.Part.from_bytes(data=audio_bytes, mime_type='audio/ogg'),
        prompt
    ])
    return clean_latex_to_unicode(response.text)


def download_file_if_possible(url: str, output_dir: str = "downloads") -> str | None:
    """Attempts to download a direct PDF or single Google Drive file."""
    os.makedirs(output_dir, exist_ok=True)
    try:
        if "drive.google.com/file/d/" in url or "drive.google.com/open?id=" in url:
            return gdown.download(url, output=f"{output_dir}/", quiet=True, fuzzy=True)
        elif url.lower().endswith(".pdf"):
            filename = os.path.join(output_dir, url.split("/")[-1].split("?")[0])
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                with open(filename, 'wb') as f:
                    f.write(r.content)
                return filename
    except Exception:
        return None
    return None


# --- TELEGRAM BOT HANDLERS ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hey! Welcome to your Study Assistant Bot.\n\n"
        "1️⃣ Send a YouTube link: I'll grab all PDFs/links from the description and generate a formatted PDF of study notes.\n"
        "2️⃣ Ask any doubt (Text or Voice Note): After sending a video, just type a question or send a voice note and I'll answer it from the lecture!\n"
        "3️⃣ Use /clear anytime to reset the current lecture memory."
    )


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🧹 Cleared the current lecture from memory! Send a new YouTube link whenever you're ready.")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    video_id = extract_video_id(text)

    # --- PATH A: User sent a YouTube Link -> Process Video & Generate PDF ---
    if video_id:
        status_msg = await update.message.reply_text("🔍 Scanning video description & unshortening links...")
        try:
            # 1. Fetch Metadata & Description Links
            metadata = await asyncio.to_thread(get_video_metadata, text)
            title = metadata['title']
            description = metadata['description']
            links = await asyncio.to_thread(extract_resource_links, description)

            links_msg = f"📌 {title}\n\n"
            has_links = False
            if links['drive']:
                has_links = True
                links_msg += "📂 Google Drive / Docs Links:\n" + "\n".join(f"• {l}" for l in links['drive']) + "\n\n"
            if links['pdfs']:
                has_links = True
                links_msg += "📄 Direct PDF Links:\n" + "\n".join(f"• {l}" for l in links['pdfs']) + "\n\n"
            if links['other']:
                has_links = True
                links_msg += "🔗 Other Links:\n" + "\n".join(f"• {l}" for l in links['other'][:6]) + "\n"

            if not has_links:
                links_msg += "No external PDF or Drive links found in the description."

            await status_msg.edit_text(links_msg, disable_web_page_preview=True)

            # 2. Try Auto-Downloading Direct PDFs / Single Drive Files
            for file_url in (links['pdfs'] + links['drive'])[:3]:
                filepath = await asyncio.to_thread(download_file_if_possible, file_url)
                if filepath and os.path.exists(filepath):
                    with open(filepath, 'rb') as f:
                        await update.message.reply_document(
                            document=f,
                            caption="📥 Grabbed this file directly from the description!"
                        )
                    os.remove(filepath)

            # 3. Fetch Transcript & Generate Notes
            notes_status = await update.message.reply_text("📝 Fetching transcript & compiling your PDF study notes...")
            transcript = await asyncio.to_thread(fetch_transcript, video_id)
            notes = await asyncio.to_thread(generate_study_notes, title, transcript, description)

            # Save lecture context in memory for follow-up Q&A
            context.user_data['lecture'] = {
                'title': title,
                'transcript': transcript,
                'notes': notes
            }
            context.user_data['qa_history'] = []

            # 4. Generate Styled PDF File
            safe_title = re.sub(r'[^\w\-_]', '_', title)[:40]
            pdf_filename = f"{safe_title}_Notes.pdf"
            await asyncio.to_thread(create_notes_pdf, title, notes, pdf_filename)

            await notes_status.delete()

            # Send clean, readable text in Telegram chat
            chat_friendly_notes = format_for_telegram_chat(notes)
            for i in range(0, len(chat_friendly_notes), 4000):
                await update.message.reply_text(chat_friendly_notes[i:i+4000])

            # Send the compiled PDF
            with open(pdf_filename, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    caption="📕 Here are your formatted PDF Study Notes!\n\n💡 Tip: You can now reply with any text question or voice note to ask doubts about this lecture!"
                )
            os.remove(pdf_filename)

        except Exception as e:
            await update.message.reply_text(f"⚠️ Ran into an issue: {str(e)}")
        return

    # --- PATH B: User sent a Text Question -> Answer from Lecture Context ---
    if 'lecture' not in context.user_data:
        await update.message.reply_text(
            "🎥 Send me a YouTube lecture link first! Once I make the notes, you can ask me any questions about it."
        )
        return

    thinking_msg = await update.message.reply_text("🤔 Thinking...")
    try:
        lecture_data = context.user_data['lecture']
        qa_history = context.user_data.get('qa_history', [])

        answer = await asyncio.to_thread(answer_text_question, text, lecture_data, qa_history)
        qa_history.append((text, answer))
        context.user_data['qa_history'] = qa_history

        clean_answer = format_for_telegram_chat(answer)
        await thinking_msg.delete()
        for i in range(0, len(clean_answer), 4000):
            await update.message.reply_text(clean_answer[i:i+4000])

    except Exception as e:
        await thinking_msg.edit_text(f"⚠️ Couldn't process your question: {str(e)}")


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows her to ask doubts via Telegram Voice Notes without typing."""
    if 'lecture' not in context.user_data:
        await update.message.reply_text("🎥 Send me a YouTube lecture link first before asking voice questions!")
        return

    thinking_msg = await update.message.reply_text("🎧 Listening to your question...")
    try:
        voice_file = await update.message.voice.get_file()
        audio_bytes = await voice_file.download_as_bytearray()

        lecture_data = context.user_data['lecture']
        answer = await asyncio.to_thread(answer_voice_question, bytes(audio_bytes), lecture_data)

        clean_answer = format_for_telegram_chat(answer)
        await thinking_msg.delete()
        for i in range(0, len(clean_answer), 4000):
            await update.message.reply_text(clean_answer[i:i+4000])

    except Exception as e:
        await thinking_msg.edit_text(f"⚠️ Couldn't process voice note: {str(e)}")


# --- 24/7 KEEP-ALIVE SERVER (FOR CLOUD HOSTING) ---

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Study Bot is alive and running 24/7!")

    def log_message(self, format, *args):
        pass  # Silences HTTP logs so your console stays clean


def start_keep_alive_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


def main():
    # Starts background web server so Render/UptimeRobot can keep the bot awake 24/7
    threading.Thread(target=start_keep_alive_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))

    print("🚀 Study Bot is running 24/7! Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
