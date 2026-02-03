import re
import io
import zipfile
from datetime import datetime

import streamlit as st
from pypdf import PdfReader, PdfWriter


# -------------------------
# Helpers
# -------------------------
def safe_filename(s: str, max_len: int = 120) -> str:
    s = (s or "").strip()
    s = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", s)  # Windows forbidden chars
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" ._")
    return s[:max_len] if len(s) > max_len else s


def extract_tax_invoice_no(page_text: str) -> str | None:
    """
    Extracts invoice number from text like:
      'Tax Invoice No: 1007585'
    Handles minor formatting variations.
    """
    text = (page_text or "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)

    # Flexible match:
    # Tax Invoice No: 1007585
    # Tax Invoice No 1007585
    # Tax Invoice No. 1007585
    m = re.search(
        r"\bTax\s+Invoice\s+No\.?\s*:?\s*([0-9]{4,})\b",
        text,
        flags=re.IGNORECASE,
    )
    return m.group(1) if m else None


def split_pdf_to_zip(pdf_bytes: bytes, skip_unmatched: bool) -> bytes:
    reader = PdfReader(io.BytesIO(pdf_bytes))

    zip_buffer = io.BytesIO()
    used_names = set()

    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            inv_no = extract_tax_invoice_no(text)

            if not inv_no and skip_unmatched:
                continue

            base = inv_no if inv_no else f"unmatched_page_{i:02d}"
            filename = safe_filename(base) + ".pdf"

            # If same invoice number appears more than once, avoid overwriting
            key = filename.lower()
            if key in used_names:
                filename = safe_filename(base) + f"__p{i:02d}.pdf"
            used_names.add(filename.lower())

            writer = PdfWriter()
            writer.add_page(page)

            out_pdf = io.BytesIO()
            writer.write(out_pdf)
            out_pdf.seek(0)

            zf.writestr(filename, out_pdf.read())

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
    reader = PdfReader(io.BytesIO(uploaded.getvalue()))

    st.subheader("Preview (first 8 pages)")
    preview = []
    for i in range(1, min(8, len(reader.pages)) + 1):
        text = reader.pages[i - 1].extract_text() or ""
        inv_no = extract_tax_invoice_no(text)
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
        zip_bytes = split_pdf_to_zip(uploaded.getvalue(), skip_unmatched=skip_unmatched)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        st.download_button(
            "Download ZIP",
            data=zip_bytes,
            file_name=f"split_pages_{ts}.zip",
            mime="application/zip",
        )
else:
    st.info("Upload a PDF to begin.")