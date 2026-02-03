import re
import io
import zipfile
from datetime import datetime
from typing import Optional, List

import streamlit as st

import pdfplumber
from pypdf import PdfReader, PdfWriter

# OCR / image deps (used only if needed)
from pdf2image import convert_from_bytes
from PIL import Image
import numpy as np
import easyocr


# -------------------------
# Helpers
# -------------------------
def safe_filename(s: str, max_len: int = 120) -> str:
    s = (s or "").strip()
    s = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", s)  # Windows forbidden chars
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" ._")
    return s[:max_len] if len(s) > max_len else s


def extract_tax_invoice_no(text: str) -> Optional[str]:
    """
    Extract invoice number from text like:
      'Tax Invoice No: 1007585'

    More tolerant:
      - handles weird spacing/newlines
      - handles digits that may be spaced: "1 0 0 7 5 8 5"
    """
    text = (text or "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)

    # Capture digits that might contain internal spaces
    m = re.search(
        r"\bTax\s+Invoice\s+No\.?\s*:?\s*([0-9][0-9\s]{3,})\b",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return None

    digits = re.sub(r"\s+", "", m.group(1))
    return digits if digits.isdigit() else None


def get_invoice_numbers_by_page_pdfplumber(pdf_bytes: bytes) -> List[Optional[str]]:
    """
    Best-effort per-page invoice number extraction using pdfplumber.
    """
    invs: List[Optional[str]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            invs.append(extract_tax_invoice_no(page_text))
    return invs


@st.cache_resource
def get_ocr_reader():
    # Cached so it doesn't reload models every run
    return easyocr.Reader(["en"], gpu=False)


def ocr_page_image(image: Image.Image, reader: easyocr.Reader) -> str:
    """
    OCR a PIL image via EasyOCR (requires numpy array input).
    """
    img_np = np.array(image.convert("RGB"))
    results = reader.readtext(img_np)
    return " ".join([r[1] for r in results if r and len(r) > 1])


def page_image_to_single_page_pdf_bytes(image: Image.Image) -> bytes:
    """
    Save a PIL image as a 1-page PDF (bytes).
    Note: OCR fallback outputs "image PDFs" (not vector).
    """
    buf = io.BytesIO()
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.save(buf, format="PDF")
    buf.seek(0)
    return buf.read()


def build_unique_filename(base: str, used_names: set, page_num: int) -> str:
    """
    base: invoice number or unmatched label
    returns a unique filename with .pdf extension
    """
    base = safe_filename(base)
    filename = f"{base}.pdf"
    key = filename.lower()

    if key in used_names:
        filename = f"{base}__p{page_num:02d}.pdf"
        key = filename.lower()

    used_names.add(key)
    return filename


def split_pdf_to_zip(pdf_bytes: bytes, skip_unmatched: bool, force_ocr: bool) -> bytes:
    """
    Strategy:
      1) Use pdfplumber per-page text extraction (best for invoices)
      2) If still no hits anywhere OR force_ocr=True, fall back to OCR
    """
    zip_buffer = io.BytesIO()
    used_names = set()

    # Text-first using pdfplumber
    invs = []
    if not force_ocr:
        invs = get_invoice_numbers_by_page_pdfplumber(pdf_bytes)

    any_found = any(invs) if invs else False

    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if any_found and not force_ocr:
            # Split using pypdf (keeps original PDF quality)
            reader = PdfReader(io.BytesIO(pdf_bytes))

            for i, page in enumerate(reader.pages, start=1):
                inv_no = invs[i - 1] if (i - 1) < len(invs) else None

                if not inv_no and skip_unmatched:
                    continue

                base = inv_no if inv_no else f"unmatched_page_{i:02d}"
                filename = build_unique_filename(base, used_names, i)

                writer = PdfWriter()
                writer.add_page(page)

                out_pdf = io.BytesIO()
                writer.write(out_pdf)
                out_pdf.seek(0)

                zf.writestr(filename, out_pdf.read())

        else:
            # OCR fallback (slow, but works for scanned PDFs)
            ocr_reader = get_ocr_reader()
            images = convert_from_bytes(pdf_bytes, dpi=200)

            for i, image in enumerate(images, start=1):
                ocr_text = ocr_page_image(image, ocr_reader)
                inv_no = extract_tax_invoice_no(ocr_text)

                if not inv_no and skip_unmatched:
                    continue

                base = inv_no if inv_no else f"unmatched_page_{i:02d}"
                filename = build_unique_filename(base, used_names, i)

                zf.writestr(filename, page_image_to_single_page_pdf_bytes(image))

    zip_buffer.seek(0)
    return zip_buffer.read()


# -------------------------
# Streamlit UI
# -------------------------
st.set_page_config(page_title="PDF Splitter (Tax Invoice No filenames)", layout="wide")
st.title("PDF Splitter — filename = Tax Invoice No (e.g. 1007585.pdf)")

uploaded = st.file_uploader("Upload PDF", type=["pdf"])
skip_unmatched = st.checkbox("Skip pages without a Tax Invoice No", value=False)
force_ocr = st.checkbox("Force OCR (slower — only for scanned PDFs)", value=False)

if uploaded:
    pdf_bytes = uploaded.getvalue()

    st.subheader("Preview (first 8 pages)")
    preview = []

    # Preview uses pdfplumber (more reliable than pypdf for invoice text)
    invs = get_invoice_numbers_by_page_pdfplumber(pdf_bytes)

    for i in range(1, min(8, len(invs)) + 1):
        inv_no = invs[i - 1]
        preview.append(
            {
                "Page": i,
                "Detected Tax Invoice No": inv_no or ("(will OCR on split)" if force_ocr else "(none)"),
                "Output filename": f"{inv_no}.pdf" if inv_no else f"unmatched_page_{i:02d}.pdf",
            }
        )

    st.dataframe(preview, use_container_width=True)

    st.divider()
    if st.button("Split and create ZIP", type="primary"):
        try:
            zip_bytes = split_pdf_to_zip(
                pdf_bytes,
                skip_unmatched=skip_unmatched,
                force_ocr=force_ocr,
            )
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            st.download_button(
                "Download ZIP",
                data=zip_bytes,
                file_name=f"split_pages_{ts}.zip",
                mime="application/zip",
            )
        except Exception as e:
            st.error(f"Error: {e}")
else:
    st.info("Upload a PDF to begin.")