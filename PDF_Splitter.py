import re
import io
import zipfile
from datetime import datetime

import streamlit as st
import easyocr
from pdf2image import convert_from_bytes
from PIL import Image
import numpy as np


# -------------------------
# Helpers
# -------------------------
def safe_filename(s: str, max_len: int = 120) -> str:
    s = (s or "").strip()
    s = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", s)  # Windows forbidden chars
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" ._")
    return s[:max_len] if len(s) > max_len else s


@st.cache_resource
def get_ocr_reader():
    # gpu=False keeps it compatible in most hosted environments
    return easyocr.Reader(["en"], gpu=False)


def extract_tax_invoice_no_from_ocr_text(ocr_text: str) -> str | None:
    """
    Pulls invoice number from OCR text like:
      'Tax Invoice No: 1007585'
    Accepts minor variations:
      - Tax Invoice No 1007585
      - Tax Invoice No. 1007585
      - spacing/colon variations
    """
    if not ocr_text:
        return None

    # Normalize some common OCR quirks
    text = ocr_text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)

    m = re.search(
        r"\bTax\s+Invoice\s+No\.?\s*:?\s*([0-9]{4,})\b",
        text,
        flags=re.IGNORECASE,
    )
    return m.group(1) if m else None


def ocr_page_image(image: Image.Image, reader: easyocr.Reader) -> str:
    """
    OCR the whole page. Returns a single combined text string.
    """
    img_np = np.array(image.convert("RGB"))  # PIL -> numpy array (supported by easyocr)
    results = reader.readtext(img_np)
    return " ".join([r[1] for r in results if r and len(r) > 1])


def page_image_to_single_page_pdf_bytes(image: Image.Image) -> bytes:
    """
    Save a PIL image as a 1-page PDF (bytes).
    """
    buf = io.BytesIO()
    # Convert to RGB to avoid 'cannot save mode RGBA as PDF' issues
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.save(buf, format="PDF")
    buf.seek(0)
    return buf.read()


def split_pdf_to_zip(pdf_bytes: bytes, skip_unmatched: bool) -> bytes:
    """
    Convert PDF->images, OCR each page, name file by Tax Invoice No, zip results.
    """
    reader = get_ocr_reader()

    # Convert PDF pages to images
    # dpi=200 is a good balance of speed vs OCR accuracy
    images = convert_from_bytes(pdf_bytes, dpi=200)

    zip_buffer = io.BytesIO()
    used_names = set()

    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, image in enumerate(images, start=1):
            ocr_text = ocr_page_image(image, reader)
            inv_no = extract_tax_invoice_no_from_ocr_text(ocr_text)

            if not inv_no and skip_unmatched:
                continue

            base = inv_no if inv_no else f"unmatched_page_{i:02d}"
            filename = safe_filename(base) + ".pdf"

            # Avoid overwriting duplicates
            key = filename.lower()
            if key in used_names:
                filename = safe_filename(base) + f"__p{i:02d}.pdf"
            used_names.add(filename.lower())

            page_pdf_bytes = page_image_to_single_page_pdf_bytes(image)
            zf.writestr(filename, page_pdf_bytes)

    zip_buffer.seek(0)
    return zip_buffer.read()


# -------------------------
# Streamlit UI
# -------------------------
st.set_page_config(page_title="PDF Splitter (Tax Invoice No filenames)", layout="wide")
st.title("PDF Splitter — filename = Tax Invoice No (e.g. 1007585.pdf)")

uploaded = st.file_uploader("Upload PDF", type=["pdf"])
skip_unmatched = st.checkbox("Skip pages without a Tax Invoice No", value=False)

if uploaded:
    pdf_bytes = uploaded.getvalue()

    # Preview: OCR first 8 pages only
    st.subheader("Preview (first 8 pages)")
    preview = []

    # Convert only first few pages for preview to keep it snappy
    preview_images = convert_from_bytes(pdf_bytes, dpi=200, first_page=1, last_page=8)
    ocr_reader = get_ocr_reader()

    for i, image in enumerate(preview_images, start=1):
        ocr_text = ocr_page_image(image, ocr_reader)
        inv_no = extract_tax_invoice_no_from_ocr_text(ocr_text)

        preview.append(
            {
                "Page": i,
                "Detected Tax Invoice No": inv_no or "(none)",
                "Output filename": f"{inv_no}.pdf" if inv_no else f"unmatched_page_{i:02d}.pdf",
            }
        )

    st.dataframe(preview, use_container_width=True)

    st.divider()
    if st.button("Split and create ZIP", type="primary"):
        try:
            zip_bytes = split_pdf_to_zip(pdf_bytes, skip_unmatched=skip_unmatched)
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