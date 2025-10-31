"""
Flask app that ranks uploaded resumes against a job description using
semantic embeddings (Sentence-BERT) + cosine similarity. Also extracts
a short highlighted snippet from each resume that best matches the JD.

Requires: sentence-transformers, sklearn, PyPDF2, docx2txt, Flask
"""
import os
import re
import tempfile
from flask import Flask, request, render_template, redirect, url_for
from werkzeug.utils import secure_filename
import PyPDF2
import docx2txt
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

# Try to import sentence-transformers; provide clear error if missing
try:
    from sentence_transformers import SentenceTransformer
except Exception as e:
    raise RuntimeError(
        "sentence-transformers is required. Install with: pip install sentence-transformers"
    ) from e

# Config
ALLOWED_EXTENSIONS = {'pdf', 'docx', 'txt'}
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.path.dirname(__file__), 'uploads'))
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB limit for uploads

# Load embedding model once (small & fast model suitable for production/testing)
MODEL_NAME = "all-MiniLM-L6-v2"
model = SentenceTransformer(MODEL_NAME)

# ---------- Helpers ----------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_pdf(path):
    text = []
    try:
        with open(path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for p in reader.pages:
                page_text = p.extract_text()
                if page_text:
                    text.append(page_text)
    except Exception:
        # return what we've got or empty string
        pass
    return " ".join(text)

def extract_text_from_docx(path):
    try:
        return docx2txt.process(path) or ""
    except Exception:
        return ""

def extract_text_from_txt(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception:
        return ""

def extract_text(file_path):
    ext = file_path.rsplit('.', 1)[1].lower()
    if ext == 'pdf':
        return extract_text_from_pdf(file_path)
    elif ext == 'docx':
        return extract_text_from_docx(file_path)
    elif ext == 'txt':
        return extract_text_from_txt(file_path)
    return ""

def clean_text(text):
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text)  # collapse whitespace
    text = text.strip()
    return text

def split_sentences(text, max_sent=200):
    # simple sentence splitter (fast, no heavy NLP dependency)
    candidates = re.split(r'(?<=[.!?])\s+', text)
    # keep length reasonable
    if len(candidates) > max_sent:
        return candidates[:max_sent]
    return candidates

def top_sentence_snippet(resume_text, job_embedding, top_k=1):
    """
    Find the sentence(s) in resume_text that are most similar to job_embedding.
    Returns a short snippet (string).
    """
    sentences = split_sentences(clean_text(resume_text))
    if not sentences:
        return ""
    try:
        sent_embeddings = model.encode(sentences, convert_to_numpy=True)
        # compute cosine similarity between job and each sentence
        sims = cosine_similarity([job_embedding], sent_embeddings)[0]
        best_idx = int(np.argmax(sims))
        snippet = sentences[best_idx].strip()
        # Trim snippet to reasonable length
        if len(snippet) > 280:
            snippet = snippet[:277].rstrip() + "..."
        return snippet
    except Exception:
        # fallback
        return sentences[0][:280].strip()

def compute_embeddings(texts):
    """
    Compute embeddings for a list of texts using the shared model.
    Returns a numpy array of shape (n_texts, dim).
    """
    return model.encode(texts, convert_to_numpy=True)

# ---------- Routes ----------
@app.route("/", methods=['GET'])
def index():
    return render_template('matchresume.html')

@app.route("/matcher", methods=['POST'])
def matcher():
    job_description = request.form.get('job_description', '').strip()
    files = request.files.getlist('resumes')

    if not job_description:
        return render_template('matchresume.html', message="Please enter a job description.", results=None)

    if not files or all(f.filename == '' for f in files):
        return render_template('matchresume.html', message="Please upload one or more resume files.", results=None)

    saved_files = []
    resumes_raw_texts = []
    file_names = []

    # Save uploaded files and extract text
    for f in files:
        if f and allowed_file(f.filename):
            filename = secure_filename(f.filename)
            # avoid overwriting
            base, ext = os.path.splitext(filename)
            counter = 1
            save_name = filename
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], save_name)
            while os.path.exists(save_path):
                save_name = f"{base}_{counter}{ext}"
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], save_name)
                counter += 1
            try:
                f.save(save_path)
                saved_files.append(save_path)
                file_names.append(save_name)
                text = extract_text(save_path)
                resumes_raw_texts.append(clean_text(text))
            except Exception:
                # skip file on error
                continue
        else:
            continue

    if len(resumes_raw_texts) == 0:
        return render_template('matchresume.html', message="No supported resume files were uploaded.", results=None)

    # Compute embeddings
    try:
        job_embedding = compute_embeddings([job_description])[0]  # shape (dim,)
        resume_embeddings = compute_embeddings(resumes_raw_texts)  # shape (n, dim)
    except Exception as e:
        return render_template('matchresume.html', message=f"Embedding error: {e}", results=None)

    # Compute similarities
    sims = cosine_similarity([job_embedding], resume_embeddings)[0]  # (n,)
    # Build results list
    results = []
    for name, score, raw_text in zip(file_names, sims, resumes_raw_texts):
        percent = round(float(score) * 100, 2)
        snippet = top_sentence_snippet(raw_text, job_embedding)
        results.append({
            "filename": name,
            "score": percent,
            "snippet": snippet
        })

    results_sorted = sorted(results, key=lambda x: x['score'], reverse=True)
    best = results_sorted[0] if results_sorted else None

    return render_template(
        'matchresume.html',
        message="Matching results:",
        results=results_sorted,
        best=best,
        job_description=job_description
    )

# ---------- Run ----------
if __name__ == "__main__":
    # For development; in production use a WSGI server
    app.run(host="0.0.0.0", port=5000, debug=True)
