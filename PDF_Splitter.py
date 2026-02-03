import re
import io
import zipfile
from datetime import datetime

import streamlit as st
from pypdf import PdfReader, PdfWriter

# OCR deps are optional – only used if we must
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


def extract_tax_invoice_no_from_text(text: str) -> str | None:
    text = (text or "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)

    m = re.search(
        r"\bTax\s+Invoice\s+No\.?\s*:?\s*([0-9]{4,})\b",
        text,
        flags=re.IGNORECASE,
    )
    return m.group(1) if m else None


@st.cache_resource
def get_ocr_reader():
    return easyocr.Reader(["en"], gpu=False)


def ocr_page_image(image: Image.Image, reader: easyocr.Reader) -> str:
    img_np = np.array(image.convert("RGB"))
    results = reader.readtext(img_np)
    return " ".join([r[1] for r in results if r and len(r) > 1])


def page_image_to_single_page_pdf_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.save(buf, format="PDF")
    buf.seek(0)
    return buf.read()


def split_pdf_to_zip(pdf_bytes: bytes, skip_unmatched: bool, force_ocr: bool) -> bytes:
    """
    Strategy:
      1) Try PDF text extraction per page (fast)
      2) If nothing found anywhere (or force_ocr=True), OCR pages (slow)
    """
    used_names = set()
    zip_buffer = io.BytesIO()

    # ---------
    # Pass 1: Text extraction
    # ---------
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text_hits = []
    if not force_ocr:
        for page in reader.pages:
            inv = extract_tax_invoice_no_from_text(page.extract_text() or "")
            text_hits.append(inv)

    any_text_found = any(text_hits)

    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if any_text_found and not force_ocr:
            # Write original page PDFs (keeps vector quality)
            for i, page in enumerate(reader.pages, start=1):
                inv_no = text_hits[i - 1]

                if not inv_no and skip_unmatched:
                    continue

                base = inv_no if inv_no else f"unmatched_page_{i:02d}"
                filename = safe_filename(base) + ".pdf"

                if filename.lower() in used_names:
                    filename = safe_filename(base) + f"__p{i:02d}.pdf"
                used_names.add(filename.lower())

                w = PdfWriter()
                w.add_page(page)
                out_pdf = io.BytesIO()
                w.write(out_pdf)
                out_pdf.seek(0)
                zf.writestr(filename, out_pdf.read())

        else:
            # ---------
            # Pass 2: OCR fallback
            # ---------
            ocr_reader = get_ocr_reader()
            images = convert_from_bytes(pdf_bytes, dpi=200)

            for i, image in enumerate(images, start=1):
                ocr_text = ocr_page_image(image, ocr_reader)
                inv_no = extract_tax_invoice_no_from_text(ocr_text)

                if not inv_no and skip_unmatched:
                    continue

                base = inv_no if inv_no else f"unmatched_page_{i:02d}"
                filename = safe_filename(base) + ".pdf"

                if filename.lower() in used_names:
                    filename = safe_filename(base) + f"__p{i:02d}.pdf"
                used_names.add(filename.lower())

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
force_ocr = st.checkbox("Force OCR (slower, use only for scanned PDFs)", value=False)

if uploaded:
    pdf_bytes = uploaded.getvalue()

    st.subheader("Preview (first 8 pages)")
    preview = []

    # Preview: text-first
    reader = PdfReader(io.BytesIO(pdf_bytes))
    for i in range(1, min(8, len(reader.pages)) + 1):
        inv_no = None

        if not force_ocr:
            inv_no = extract_tax_invoice_no_from_text(reader.pages[i - 1].extract_text() or "")

        # If not found and forcing OCR, show placeholder – OCR happens on split
        preview.append(
            {
                "Page": i,
                "Detected Tax Invoice No": inv_no or ("(OCR on split)" if force_ocr else "(none)"),
                "Output filename": f"{inv_no}.pdf" if inv_no else f"unmatched_page_{i:02d}.pdf",
            }
        )

    st.dataframe(preview, use_container_width=True)

    st.divider()
    if st.button("Split and create ZIP", type="primary"):
        try:
            zip_bytes = split_pdf_to_zip(pdf_bytes, skip_unmatched=skip_unmatched, force_ocr=force_ocr)
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