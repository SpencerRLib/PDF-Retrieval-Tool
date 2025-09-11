# PDF Retriever

A Python tool for processing RIS bibliography files, retrieving missing DOIs, and downloading academic PDFs with institutional proxy support.

## Features

- **RIS File Processing**: Parse and process RIS bibliography files
- **DOI Retrieval**: Automatically find missing DOIs using CrossRef, PubMed, and Semantic Scholar APIs
- **PDF Download**: Download PDFs from multiple sources including open access repositories
- **Institutional Proxy Support**: Access paywalled content through library proxies
- **Library Linkresolver**: OpenURL support for library systems
- **GUI Interface**: User-friendly Tkinter interface (`gui.py`)
- **Command Line**: Script-based processing (`retriever.py`)

## Requirements

```bash
pip install requests
```

## Quick Start

### GUI Version
```bash
python gui.py
```

### Command Line Version
```bash
python retriever.py
```

Place your RIS files in the same directory as the scripts.

## Configuration

- **Proxy URL**: Format like `http://proxy.library.upei.ca/login?qurl=%u`
- **Linkresolver**: OpenURL resolver like `https://resolver-ebscohost-com.proxy.library.upei.ca/openurl?`

## Output

- `updated_bibliography.ris` - Original RIS file with retrieved DOIs and PDF attachments
- `files/` - Downloaded PDF files with descriptive names
- `pdf_retrieval_report.txt` - Detailed processing results

## Supported Sources

- Unpaywall (open access)
- arXiv preprints
- PubMed Central
- Publisher direct links
- CrossRef metadata
- Library linkresolvers

## Notes

The tool respects rate limits and includes delays to avoid overwhelming APIs. Success rates vary depending on institutional access and open access availability.
