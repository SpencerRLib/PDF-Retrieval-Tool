#!/usr/bin/env python3
"""
Academic PDF Retrieval Script with RIS Processing and DOI Recovery
Processes RIS files, retrieves missing DOIs, and downloads PDFs with library proxy and linkresolver support

v3 - added linkresolver support (finally!)
TODO: cleanup the messy crossref code someday
"""

import requests
import time
import os
import re
from urllib.parse import urljoin, urlparse, quote, urlencode
from pathlib import Path
import logging
from typing import List, Dict, Optional, Tuple
import json
from dataclasses import dataclass
import difflib

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@dataclass
class ProxyConfig:
    """Configuration for institutional proxy"""
    proxy_url: str  # e.g., "https://proxy.lib.university.edu/login?url="
    username: Optional[str] = None
    password: Optional[str] = None
    cookies: Optional[Dict] = None

@dataclass
class RISEntry:
    """Represents a single RIS bibliography entry"""
    entry_type: str = ""
    title: str = ""
    authors: List[str] = None
    journal: str = ""
    year: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    doi: str = ""
    pmid: str = ""
    isbn: str = ""
    publisher: str = ""
    abstract: str = ""
    keywords: List[str] = None
    url: str = ""
    # Local filesystem path to downloaded PDF (relative to RIS location)
    attachment_path: str = ""
    raw_entry: str = ""
    
    def __post_init__(self):
        if self.authors is None:
            self.authors = []
        if self.keywords is None:
            self.keywords = []

class RISParser:
    """Parser for RIS bibliography files - handles most common formats"""
    
    def __init__(self):
        # mapping of RIS tags to our internal fields
        # some tags are redundant but different sources use diff ones
        self.field_mapping = {
            'TY': 'entry_type',
            'TI': 'title',
            'T1': 'title',  # Alternative title field
            'AU': 'authors',
            'A1': 'authors',  # Alternative author field
            'JO': 'journal',
            'JF': 'journal',  # Full journal name
            'T2': 'journal',  # Alternative journal field
            'PY': 'year',
            'Y1': 'year',
            'VL': 'volume',
            'IS': 'issue',
            'SP': 'pages',
            'EP': 'pages',  # End page - will be combined with start page
            'DO': 'doi',
            'PM': 'pmid',
            'SN': 'isbn',
            'PB': 'publisher',
            'AB': 'abstract',
            'N2': 'abstract',  # Alternative abstract field
            'KW': 'keywords',
            'UR': 'url',
            'L1': 'url',  # Alternative URL field
        }
    
    def parse_ris_file(self, filepath: str) -> List[RISEntry]:
        """Parse RIS file and return list of entries"""
        logger.info(f"Parsing RIS file: {filepath}")
        
        entries = []
        current_entry = None
        current_raw = []
        
        # try UTF-8 first, then fallback to latin-1 if that fails
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except UnicodeDecodeError:
            # Try with different encoding - some old RIS files are weird
            with open(filepath, 'r', encoding='latin-1', errors='replace') as f:
                lines = f.readlines()
        
        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            current_raw.append(line)
            
            if not line:
                continue
            
            # Start of new entry
            if line.startswith('TY  -'):
                if current_entry:
                    current_entry.raw_entry = '\n'.join(current_raw[:-1])
                    entries.append(current_entry)
                current_entry = RISEntry()
                current_raw = [line]
            
            # End of entry
            elif line.startswith('ER  -'):
                if current_entry:
                    current_entry.raw_entry = '\n'.join(current_raw)
                    entries.append(current_entry)
                    current_entry = None
                    current_raw = []
            
            # Parse field
            elif '  - ' in line and current_entry:
                try:
                    field, value = line.split('  - ', 1)
                    field = field.strip()
                    value = value.strip()
                    
                    self._add_field_to_entry(current_entry, field, value)
                    
                except ValueError:
                    logger.warning(f"Could not parse line {line_num}: {line}")
        
        # Handle last entry if file doesn't end with ER
        if current_entry:
            current_entry.raw_entry = '\n'.join(current_raw)
            entries.append(current_entry)
        
        logger.info(f"Parsed {len(entries)} entries from RIS file")
        return entries
    
    def _add_field_to_entry(self, entry: RISEntry, field: str, value: str):
        """Add a field value to a RIS entry"""
        if field not in self.field_mapping:
            return
        
        attr_name = self.field_mapping[field]
        
        if attr_name == 'authors' or attr_name == 'keywords':
            # Multi-value fields
            current_list = getattr(entry, attr_name)
            current_list.append(value)
        elif attr_name == 'pages':
            # Handle page ranges - this is kinda hacky but works
            current_pages = getattr(entry, attr_name)
            if field == 'SP':  # Start page
                setattr(entry, attr_name, value)
            elif field == 'EP' and current_pages:  # End page
                setattr(entry, attr_name, f"{current_pages}-{value}")
        else:
            # Single value fields - only set if not already set
            if not getattr(entry, attr_name):
                setattr(entry, attr_name, value)

class DOIRetriever:
    """Retrieves missing DOIs for bibliography entries
    
    Uses multiple APIs to try and find DOIs
    TODO: maybe add semantic scholar v2 API later
    """
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Academic Research Tool) AppleWebKit/537.36'
        })
        
        # DOI retrieval strategies - tried in order
        self.doi_strategies = [
            self._search_crossref,
            self._search_pubmed,
            self._search_semantic_scholar,
            self._search_google_scholar_fallback  # this doesn't really work yet
        ]
    
    def retrieve_missing_dois(self, entries: List[RISEntry], max_retries: int = 2) -> List[RISEntry]:
        """Retrieve DOIs for entries that are missing them"""
        logger.info("Scanning for entries with missing DOIs...")
        
        missing_doi_entries = [entry for entry in entries if not entry.doi.strip()]
        logger.info(f"Found {len(missing_doi_entries)} entries missing DOIs")
        
        if not missing_doi_entries:
            return entries
        
        logger.info("Starting DOI retrieval process...")
        
        retrieved_count = 0
        for i, entry in enumerate(missing_doi_entries):
            logger.info(f"Retrieving DOI {i+1}/{len(missing_doi_entries)}: {entry.title[:50]}...")
            
            found_doi = None
            # Try each strategy to find a DOI
            for strategy in self.doi_strategies:
                try:
                    doi = strategy(entry)
                    if doi:
                        logger.info(f"Found DOI via {strategy.__name__}: {doi}")
                        found_doi = doi
                        break # Stop after the first successful strategy
                except Exception as e:
                    logger.warning(f"Strategy {strategy.__name__} failed: {e}")
                    continue
            
            # **CRITICAL FIX**: Only assign the found DOI to the entry object.
            # This ensures no other metadata is accidentally modified.
            if found_doi:
                entry.doi = found_doi
                retrieved_count += 1
            else:
                logger.warning(f"Could not retrieve DOI for: {entry.title[:50]}...")
            
            # Respectful delay - don't hammer the APIs
            time.sleep(1.0)
        
        logger.info(f"Successfully retrieved {retrieved_count}/{len(missing_doi_entries)} missing DOIs")
        
        return entries
    
    def _search_crossref(self, entry: RISEntry) -> Optional[str]:
        """
        Search CrossRef for DOI using title and metadata.
        This function is read-only regarding the 'entry' object to prevent
        unintentional data enrichment that could corrupt original metadata.
        
        crossref API is pretty good but sometimes returns weird results
        """
        if not entry.title:
            return None
        
        # Build search query
        query_parts = [entry.title]
        if entry.authors:
            query_parts.append(entry.authors[0].split(',')[0])  # Last name of first author
        
        query = ' '.join(query_parts)
        
        url = "https://api.crossref.org/works"
        params = {
            'query': query,
            'rows': 5,
            # We only need DOI, title, and year for validation.
            'select': 'DOI,title,published-print,published-online'
        }
        
        response = self.session.get(url, params=params, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        
        for item in data.get('message', {}).get('items', []):
            # Check title similarity to ensure we have the right article
            api_title = ' '.join(item.get('title', []))
            # Use a high threshold to avoid false positives
            similarity = difflib.SequenceMatcher(None, 
                                               entry.title.lower().strip(), 
                                               api_title.lower().strip()).ratio()
            
            if similarity > 0.9:
                # Additional validation with year if available in both sources
                if entry.year:
                    pub_date = item.get('published-print') or item.get('published-online')
                    if pub_date and 'date-parts' in pub_date and pub_date['date-parts'][0][0]:
                        api_year = str(pub_date['date-parts'][0][0])
                        # Allow for a one-year difference
                        if abs(int(entry.year) - int(api_year)) > 1:
                            continue # Year mismatch, likely not the correct entry
                
                # If validation passes, return ONLY the DOI string
                return item['DOI']
        
        return None

    def _search_pubmed(self, entry: RISEntry) -> Optional[str]:
        """Search PubMed for DOI using PMID or title"""
        
        if entry.pmid:
            return self._get_doi_from_pmid(entry.pmid)
        
        if not entry.title:
            return None
        
        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        search_params = {'db': 'pubmed', 'term': f'"{entry.title}"[Title]', 'retmode': 'json', 'retmax': 5}
        
        if entry.authors:
            first_author = entry.authors[0].split(',')[0].strip()
            search_params['term'] += f' AND "{first_author}"[Author]'
        
        response = self.session.get(search_url, params=search_params, timeout=10)
        response.raise_for_status()
        
        search_data = response.json()
        pmids = search_data.get('esearchresult', {}).get('idlist', [])
        
        if not pmids:
            return None
        
        for pmid in pmids:
            doi = self._get_doi_from_pmid(pmid)
            if doi:
                return doi
        
        return None
    
    def _get_doi_from_pmid(self, pmid: str) -> Optional[str]:
        """Get DOI from PMID using PubMed API"""
        detail_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        detail_params = {'db': 'pubmed', 'id': pmid, 'retmode': 'xml'}
        
        response = self.session.get(detail_url, params=detail_params, timeout=10)
        response.raise_for_status()
        
        # regex to extract DOI from XML - not pretty but it works
        doi_match = re.search(r'<ArticleId IdType="doi">([^<]+)</ArticleId>', response.text)
        if doi_match:
            return doi_match.group(1)
        
        return None
    
    def _search_semantic_scholar(self, entry: RISEntry) -> Optional[str]:
        """Search Semantic Scholar for DOI - their API is decent"""
        if not entry.title:
            return None
        
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {'query': entry.title, 'limit': 5, 'fields': 'externalIds,title,year'}
        
        response = self.session.get(url, params=params, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        
        for paper in data.get('data', []):
            api_title = paper.get('title', '')
            similarity = difflib.SequenceMatcher(None, entry.title.lower().strip(), api_title.lower().strip()).ratio()
            
            if similarity > 0.9:
                if entry.year and paper.get('year'):
                    if abs(int(entry.year) - paper['year']) > 1:
                        continue
                
                external_ids = paper.get('externalIds', {})
                if 'DOI' in external_ids and external_ids['DOI']:
                    return external_ids['DOI']
        
        return None
    
    def _search_google_scholar_fallback(self, entry: RISEntry) -> Optional[str]:
        """Fallback method to extract DOI from Google Scholar (very basic)
        
        TODO: implement this properly - google scholar scraping is tricky
        """
        if not entry.title: return None
        logger.warning("Google Scholar fallback not fully implemented due to terms of service")
        return None

class EnhancedPDFRetriever:
    """Enhanced PDF retriever that works with RIS entries
    
    This class handles the heavy lifting of actually downloading PDFs
    Uses multiple strategies and supports institutional proxies
    """
    
    def __init__(self, proxy_config: Optional[ProxyConfig] = None, download_dir: str = "files", linkresolver_url: Optional[str] = None):
        self.proxy_config = proxy_config
        self.download_dir = Path(download_dir)
        self.linkresolver_url = linkresolver_url
        self.download_dir.mkdir(exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        
        # Set up proxy authentication if provided
        if proxy_config and proxy_config.cookies:
            self.session.cookies.update(proxy_config.cookies)
        
        # Common PDF retrieval strategies - order matters!
        self.retrieval_strategies = [
            self._try_direct_doi_resolution,
            self._try_crossref_links,
            self._try_unpaywall,
            self._try_arxiv_preprint,
            self._try_pmc_open_access,
            self._try_linkresolver,  # ADDED: New Linkresolver strategy (march 2024)
            self._try_publisher_specific
        ]

    def process_ris_entries(self, entries: List[RISEntry], max_retries: int = 3, delay: float = 1.0) -> Dict[str, Dict]:
        """Process RIS entries and retrieve PDFs for those with DOIs"""
        
        # Filter entries that have DOIs
        entries_with_dois = [entry for entry in entries if entry.doi.strip()]
        entries_without_dois = [entry for entry in entries if not entry.doi.strip()]
        
        logger.info(f"Processing {len(entries_with_dois)} entries with DOIs")
        if entries_without_dois:
            logger.info(f"Skipping {len(entries_without_dois)} entries without DOIs")
        
        results = {}
        
        for i, entry in enumerate(entries_with_dois):
            logger.info(f"Processing entry {i+1}/{len(entries_with_dois)}: {entry.title[:50]}...")
            
            # Use entry title as identifier if available, otherwise use DOI
            identifier = entry.title[:50] + "..." if entry.title else entry.doi
            
            # Clean DOI
            clean_doi = self._clean_doi(entry.doi)
            if not clean_doi:
                results[identifier] = {"status": "error", "message": "Invalid DOI format", "entry": entry}
                continue
            
            # Attempt retrieval with retries
            success = False
            last_error = None
            
            for attempt in range(max_retries):
                try:
                    result = self._retrieve_single_pdf(clean_doi, entry)
                    results[identifier] = {**result, "entry": entry}
                    success = result.get("status") == "success"
                    if success:
                        break
                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"Attempt {attempt + 1} failed for {identifier}: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(delay * (attempt + 1))  # Exponential backoff
            
            if not success:
                results[identifier] = {
                    "status": "error", 
                    "message": f"All attempts failed. Last error: {last_error}",
                    "entry": entry
                }
            
            # Respectful delay between entries - don't be a jerk to servers
            if i < len(entries_with_dois) - 1:
                time.sleep(delay)
        
        return results

    def _retrieve_single_pdf(self, doi: str, entry: RISEntry) -> Dict:
        """Attempt to retrieve a single PDF using multiple strategies"""
        
        # Get article metadata (prefer RIS data, supplement with CrossRef)
        metadata = self._combine_metadata(entry)
        
        for strategy in self.retrieval_strategies:
            try:
                logger.info(f"Trying strategy: {strategy.__name__} for {doi}")
                result = strategy(doi, metadata, entry)
                if result["status"] == "success":
                    return result
                    
            except Exception as e:
                logger.warning(f"Strategy {strategy.__name__} failed: {e}")
                continue
        
        return {"status": "failed", "message": "All retrieval strategies failed"}

    def _combine_metadata(self, entry: RISEntry) -> Dict:
        """Combine RIS entry data with CrossRef metadata
        
        This creates a unified metadata dict for use by retrieval strategies
        """
        metadata = {
            'title': [entry.title] if entry.title else [],
            'author': [{'family': author.split(',')[0].strip(), 'given': author.split(',')[1].strip() if ',' in author else ''} 
                      for author in entry.authors] if entry.authors else [],
            'container-title': [entry.journal] if entry.journal else [],
            'published-print': {'date-parts': [[int(entry.year)]]} if entry.year and entry.year.isdigit() else {},
            'publisher': entry.publisher,
            'volume': entry.volume,
            'issue': entry.issue,
            'page': entry.pages,
            'URL': entry.url,
            'PMID': entry.pmid
        }
        
        # Supplement with CrossRef if needed
        try:
            if entry.doi:
                crossref_data = self._get_crossref_metadata(entry.doi)
                # Merge data, preferring RIS data
                for key, value in crossref_data.items():
                    if key not in metadata or not metadata[key]:
                        metadata[key] = value
        except:
            pass  # ignore crossref errors - not critical
        
        return metadata

    def _get_crossref_metadata(self, doi: str) -> Dict:
        """Get metadata from CrossRef API"""
        try:
            url = f"https://api.crossref.org/works/{doi}"
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            if 'message' in data:
                return data['message']
                
        except Exception as e:
            logger.warning(f"Failed to get CrossRef metadata for {doi}: {e}")
        
        return {}

    def _clean_doi(self, doi: str) -> Optional[str]:
        """Clean and validate DOI format"""
        if not doi:
            return None
            
        # Remove common prefixes and clean
        doi = doi.strip()
        doi = re.sub(r'^(doi:|DOI:)', '', doi, flags=re.IGNORECASE)
        doi = re.sub(r'^https?://.*?doi\.org/', '', doi)
        
        # Basic DOI format validation - should start with 10.
        if re.match(r'^10\.\d{4,}/\S+$', doi):
            return doi
        return None

    def _try_direct_doi_resolution(self, doi: str, metadata: Dict, entry: RISEntry) -> Dict:
        """Try direct DOI resolution to publisher page"""
        doi_url = f"https://doi.org/{doi}"
        
        # Use proxy if configured
        target_url = doi_url
        if self.proxy_config:
            target_url = self.proxy_config.proxy_url + doi_url
        
        response = self.session.get(target_url, timeout=15, allow_redirects=True)
        response.raise_for_status()
        
        # Look for PDF links on the page
        pdf_urls = self._extract_pdf_links(response.text, response.url)
        
        for pdf_url in pdf_urls:
            try:
                # Apply proxy to the PDF URL itself if not an open access domain
                final_pdf_url = pdf_url
                if self.proxy_config and not self._is_open_access_domain(pdf_url):
                    final_pdf_url = self.proxy_config.proxy_url + pdf_url
                return self._download_pdf(final_pdf_url, doi, "direct_resolution", entry)
            except Exception as e:
                logger.warning(f"Failed to download from {pdf_url}: {e}")
                continue
        
        return {"status": "failed", "message": "No accessible PDF links found"}

    def _try_unpaywall(self, doi: str, metadata: Dict, entry: RISEntry) -> Dict:
        """Try Unpaywall API for open access versions - this is usually the best bet for OA"""
        try:
            url = f"https://api.unpaywall.org/v2/{doi}?email=retriever1337@gmail.com"
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            if data.get('is_oa') and data.get('best_oa_location'):
                pdf_url = data['best_oa_location'].get('url_for_pdf')
                if pdf_url:
                    return self._download_pdf(pdf_url, doi, "unpaywall", entry)
                    
        except Exception as e:
            logger.warning(f"Unpaywall failed for {doi}: {e}")
        
        return {"status": "failed", "message": "No open access version found via Unpaywall"}

    # --- NEW LINKRESOLVER METHODS ---
    # added these in march 2024 - finally got around to implementing proper openurl support
    def _build_openurl(self, entry: RISEntry) -> Optional[str]:
        """Construct an OpenURL for a given RIS entry."""
        if not self.linkresolver_url:
            return None

        params = {'genre': 'article'}
        if entry.title: params['atitle'] = entry.title
        if entry.journal: params['title'] = entry.journal
        if entry.year: params['date'] = entry.year
        if entry.volume: params['volume'] = entry.volume
        if entry.issue: params['issue'] = entry.issue
        if entry.pages: params['spage'] = entry.pages.split('-')[0]
        if entry.isbn: params['issn'] = entry.isbn
        if entry.doi:
            params['id'] = f"doi:{entry.doi}"

        if not params.get('title') and not params.get('id'):
            return None

        resolver_prefix = self.linkresolver_url
        if not resolver_prefix.endswith(('?', '&')):
            resolver_prefix += '?'

        return resolver_prefix + urlencode(params)

    def _try_linkresolver(self, doi: str, metadata: Dict, entry: RISEntry) -> Dict:
        """Try to find PDF via a library linkresolver."""
        if not self.linkresolver_url:
            return {"status": "failed", "message": "Linkresolver not configured"}

        openurl = self._build_openurl(entry)
        if not openurl:
            return {"status": "failed", "message": "Not enough metadata for OpenURL"}

        try:
            logger.info(f"Attempting linkresolver URL: {openurl}")
            response = self.session.get(openurl, timeout=20, allow_redirects=True)
            response.raise_for_status()

            pdf_urls = self._extract_pdf_links(response.text, response.url)
            for pdf_url in pdf_urls:
                try:
                    # Proxies are often built into resolver links, but we can try anyway
                    return self._download_pdf(pdf_url, doi, "linkresolver", entry)
                except Exception as e:
                    logger.warning(f"Linkresolver download failed for {pdf_url}: {e}")
                    continue
        except Exception as e:
            logger.error(f"Linkresolver request failed: {e}")

        return {"status": "failed", "message": "Could not find PDF via linkresolver"}
    # --- END NEW LINKRESOLVER METHODS ---

    def _download_pdf(self, pdf_url: str, doi: str, source: str, entry: RISEntry) -> Dict:
        """Download PDF from URL with enhanced filename generation
        
        This is where the magic happens - actually downloading the PDF
        """
        try:
            logger.info(f"Attempting to download PDF from: {pdf_url}")
            
            response = self.session.get(pdf_url, timeout=30, stream=True)
            response.raise_for_status()
            
            # Check if response is actually a PDF
            content_type = response.headers.get('content-type', '').lower()
            if 'application/pdf' not in content_type and 'pdf' not in content_type:
                # Check first few bytes for PDF signature
                # Use iter_content to avoid loading whole file in memory at once
                first_bytes = next(response.iter_content(10), b"")
                if not first_bytes.startswith(b'%PDF'):
                    raise ValueError(f"Response is not a PDF file (Content-Type: {content_type})")
                # Re-combine the stream if we read from it
                from itertools import chain
                response.raw = chain(iter((first_bytes,)), response.raw)

            # Generate enhanced filename using RIS data
            filename = self._generate_filename(entry, doi)
            filepath = self.download_dir / filename
            
            # Save PDF
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            # Verify file size - sometimes we get error pages instead of PDFs
            if filepath.stat().st_size < 1000:  # Less than 1KB is suspicious
                filepath.unlink()  # Delete file
                raise ValueError("Downloaded file is too small to be a valid PDF")
            
            logger.info(f"Successfully downloaded: {filepath}")
            
            # Record attachment path (relative to current working directory where RIS is saved)
            try:
                rel_path = os.path.relpath(filepath, Path.cwd())
            except Exception:
                rel_path = str(filepath)
            # Normalize to forward slashes for RIS compatibility
            rel_path = rel_path.replace('\\', '/')
            entry.attachment_path = rel_path
            # Also set URL to the PDF link if not already set
            if not entry.url:
                entry.url = pdf_url

            return {
                "status": "success",
                "filepath": str(filepath),
                "source": source,
                "url": pdf_url,
                "size_bytes": filepath.stat().st_size,
                "title": entry.title,
                "authors": entry.authors,
                "journal": entry.journal,
                "year": entry.year
            }
            
        except Exception as e:
            logger.error(f"PDF download failed from {pdf_url}: {e}")
            raise

    def _generate_filename(self, entry: RISEntry, doi: str) -> str:
        """Generate a descriptive filename from RIS entry data
        
        tries to create something human-readable rather than just DOI
        """
        parts = []
        
        # Add first author
        if entry.authors:
            first_author = entry.authors[0].split(',')[0].strip()
            # Clean author name - remove weird characters
            first_author = re.sub(r'[^\w\-_.]', '', first_author)
            parts.append(first_author)
        
        # Add year
        if entry.year:
            parts.append(entry.year)
        
        # Add title (truncated)
        if entry.title:
            title_clean = re.sub(r'[^\w\-_.\s]', '', entry.title)
            title_words = title_clean.split()[:5]  # First 5 words
            if title_words:
                parts.append('_'.join(title_words))
        
        if not parts:
            # Fallback to DOI if nothing else available
            safe_doi = re.sub(r'[^\w\-_.]', '_', doi)
            parts.append(safe_doi)
        
        filename = '_'.join(parts) + '.pdf'
        
        # Ensure filename isn't too long - filesystem limits
        if len(filename) > 200:
            filename = filename[:200] + '.pdf'
        
        return filename

    def _extract_pdf_links(self, html_content: str, base_url: str) -> List[str]:
        """Extract potential PDF links from HTML content"""
        pdf_links = []
        
        # Common PDF link patterns - this regex is a bit messy but works
        patterns = [
            r'href=["\']([^"\']*\.pdf[^"\']*)["\']',
            r'href=["\']([^"\']*pdf[^"\']*)["\']',
            r'href=["\']([^"\']*download[^"\']*)["\']'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, html_content, re.IGNORECASE)
            for match in matches:
                full_url = urljoin(base_url, match)
                if full_url not in pdf_links:
                    pdf_links.append(full_url)
        
        return pdf_links

    def _try_crossref_links(self, doi: str, metadata: Dict, entry: RISEntry) -> Dict:
        """Try using CrossRef link data"""
        if not metadata.get('link'):
            return {"status": "failed", "message": "No CrossRef links available"}
        
        for link in metadata['link']:
            if link.get('content-type') == 'application/pdf':
                pdf_url = link['URL']
                
                # Use proxy if needed
                final_pdf_url = pdf_url
                if self.proxy_config and not self._is_open_access_domain(pdf_url):
                    final_pdf_url = self.proxy_config.proxy_url + pdf_url
                
                try:
                    return self._download_pdf(final_pdf_url, doi, "crossref_links", entry)
                except Exception as e:
                    logger.warning(f"CrossRef link failed: {e}")
                    continue
        
        return {"status": "failed", "message": "No working CrossRef PDF links"}

    def _try_arxiv_preprint(self, doi: str, metadata: Dict, entry: RISEntry) -> Dict:
        """Try to find arXiv preprint - often available when published version isn't"""
        try:
            title = entry.title or (metadata.get('title', [''])[0] if metadata.get('title') else '')
            
            if title:
                # Search arXiv API
                search_url = "http://export.arxiv.org/api/query"
                params = {
                    'search_query': f'ti:"{title[:100]}"',
                    'start': 0,
                    'max_results': 5
                }
                
                response = self.session.get(search_url, params=params, timeout=10)
                response.raise_for_status()
                
                # Parse XML response - this is kinda ugly but arxiv uses XML
                if 'entry>' in response.text and 'arxiv.org/abs/' in response.text:
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(response.text)
                    
                    for xml_entry in root.findall('.//{http://www.w3.org/2005/Atom}entry'):
                        id_elem = xml_entry.find('.//{http://www.w3.org/2005/Atom}id')
                        if id_elem is not None:
                            arxiv_url = id_elem.text
                            arxiv_id = arxiv_url.split('/')[-1]
                            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
                            
                            return self._download_pdf(pdf_url, doi, "arxiv_preprint", entry)
                            
        except Exception as e:
            logger.warning(f"arXiv search failed for {doi}: {e}")
        
        return {"status": "failed", "message": "No arXiv preprint found"}

    def _try_pmc_open_access(self, doi: str, metadata: Dict, entry: RISEntry) -> Dict:
        """Try PubMed Central open access - good for biomedical papers"""
        try:
            # Try PMID first if available
            if entry.pmid:
                search_term = f'"{entry.pmid}"[PMID]'
            else:
                search_term = f'"{doi}"[DOI]'
            
            search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
            params = {
                'db': 'pmc',
                'term': search_term,
                'retmode': 'json',
                'retmax': 1
            }
            
            response = self.session.get(search_url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            if data.get('esearchresult', {}).get('idlist'):
                pmc_id = data['esearchresult']['idlist'][0]
                pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmc_id}/pdf/"
                
                return self._download_pdf(pdf_url, doi, "pmc_open_access", entry)
                
        except Exception as e:
            logger.warning(f"PMC search failed for {doi}: {e}")
        
        return {"status": "failed", "message": "No PMC open access version found"}

    def _try_publisher_specific(self, doi: str, metadata: Dict, entry: RISEntry) -> Dict:
        """Try publisher-specific PDF extraction methods
        
        Each publisher has their own special way of doing things *sigh*
        """
        publisher = metadata.get('publisher', '').lower()
        if entry.publisher:
            publisher = entry.publisher.lower()
        
        # Publisher-specific strategies
        if 'springer' in publisher or 'nature' in publisher:
            return self._try_springer_nature_pdf(doi, metadata, entry)
        elif 'elsevier' in publisher:
            return self._try_elsevier_pdf(doi, metadata, entry)
        elif 'wiley' in publisher:
            return self._try_wiley_pdf(doi, metadata, entry)
        
        return {"status": "failed", "message": f"No specific strategy for publisher: {publisher}"}

    def _try_springer_nature_pdf(self, doi: str, metadata: Dict, entry: RISEntry) -> Dict:
        """Springer/Nature specific PDF retrieval"""
        try:
            pdf_patterns = [
                f"https://link.springer.com/content/pdf/{doi}.pdf",
                f"https://www.nature.com/articles/{doi.split('/')[-1]}.pdf"
            ]
            
            for pdf_url in pdf_patterns:
                final_url = pdf_url
                if self.proxy_config:
                    final_url = self.proxy_config.proxy_url + pdf_url
                
                try:
                    return self._download_pdf(final_url, doi, "springer_nature", entry)
                except:
                    continue
                    
        except Exception as e:
            logger.warning(f"Springer/Nature strategy failed: {e}")
        
        return {"status": "failed", "message": "Springer/Nature PDF not accessible"}

    def _try_elsevier_pdf(self, doi: str, metadata: Dict, entry: RISEntry) -> Dict:
        """Elsevier specific PDF retrieval - usually doesn't work without subscription"""
        return {"status": "failed", "message": "Elsevier PDFs typically require ScienceDirect access"}

    def _try_wiley_pdf(self, doi: str, metadata: Dict, entry: RISEntry) -> Dict:
        """Wiley specific PDF retrieval"""
        try:
            pdf_url = f"https://onlinelibrary.wiley.com/doi/pdf/{doi}"
            
            final_url = pdf_url
            if self.proxy_config:
                final_url = self.proxy_config.proxy_url + pdf_url
            
            return self._download_pdf(final_url, doi, "wiley", entry)
            
        except Exception as e:
            logger.warning(f"Wiley strategy failed: {e}")
        
        return {"status": "failed", "message": "Wiley PDF not accessible"}

    def _is_open_access_domain(self, url: str) -> bool:
        """Check if URL is from a known open access domain - don't need proxy for these"""
        open_access_domains = [
            'arxiv.org',
            'biorxiv.org',
            'plos.org',
            'ncbi.nlm.nih.gov',
            'pubmed.ncbi.nlm.nih.gov',
            'europepmc.org'
        ]
        
        domain = urlparse(url).netloc.lower()
        return any(oa_domain in domain for oa_domain in open_access_domains)


def find_ris_files(directory: str = ".") -> List[str]:
    """Find RIS files in the specified directory"""
    ris_files = []
    for file in Path(directory).glob("*.ris"):
        ris_files.append(str(file))
    
    # Also check for common RIS file extensions
    for ext in ["*.RIS", "*.txt", "*.bib"]:
        for file in Path(directory).glob(ext):
            # Quick check if file might be RIS format
            try:
                with open(file, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read(1024) # Read first 1KB
                    if 'TY  -' in content:
                        ris_files.append(str(file))
            except:
                continue
    
    return list(set(ris_files))  # Remove duplicates


def save_results_report(results: Dict, output_file: str = "pdf_retrieval_report.txt"):
    """Save detailed results report - useful for debugging what went wrong"""
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("PDF Retrieval Results Report\n")
        f.write("=" * 50 + "\n\n")
        
        successful = 0
        failed = 0
        
        for identifier, result in results.items():
            entry = result.get('entry')
            status = result.get('status', 'unknown')
            
            f.write(f"Title: {identifier}\n")
            f.write(f"Status: {status}\n")
            
            if entry:
                f.write(f"Authors: {', '.join(entry.authors) if entry.authors else 'N/A'}\n")
                f.write(f"Journal: {entry.journal or 'N/A'}\n")
                f.write(f"Year: {entry.year or 'N/A'}\n")
                f.write(f"DOI: {entry.doi or 'N/A'}\n")
            
            if status == 'success':
                successful += 1
                f.write(f"File: {result.get('filepath', 'N/A')}\n")
                f.write(f"Source: {result.get('source', 'N/A')}\n")
                f.write(f"Size: {result.get('size_bytes', 0):,} bytes\n")
            else:
                failed += 1
                f.write(f"Error: {result.get('message', 'Unknown error')}\n")
            
            f.write("-" * 30 + "\n")
        
        total = successful + failed
        f.write(f"\nSUMMARY:\n")
        f.write(f"Successful downloads: {successful}\n")
        f.write(f"Failed downloads: {failed}\n")
        if total > 0:
            f.write(f"Success rate: {successful/total*100:.1f}%\n")


def save_updated_ris(entries: List[RISEntry], output_file: str):
    """
    Save RIS entries, preserving original formatting and injecting new DOIs or attachment paths.
    This non-destructive approach prevents accidental metadata corruption.
    
    UPDATE: fixed the issue where L1 fields were getting duplicated (april 2024)
    """
    logger.info(f"Saving updated bibliography to: {output_file}")
    with open(output_file, 'w', encoding='utf-8') as f:
        for entry in entries:
            if not entry.raw_entry:
                # Fallback for entries created on-the-fly (should be rare)
                f.write(f"TY  - {entry.entry_type or 'JOUR'}\n")
                if entry.title: f.write(f"TI  - {entry.title}\n")
                if entry.doi: f.write(f"DO  - {entry.doi}\n")
                if entry.attachment_path: f.write(f"L1  - {entry.attachment_path}\n")
                f.write("ER  - \n\n")
                continue

            lines = entry.raw_entry.strip().split('\n')
            
            # Find the end of the entry to inject new tags before it
            er_index = -1
            for i, line in enumerate(lines):
                if line.startswith('ER  -'):
                    er_index = i
                    break
            
            insert_point = er_index if er_index != -1 else len(lines)

            # --- Inject or Update DOI ---
            if entry.doi:
                doi_line_exists = False
                for i, line in enumerate(lines):
                    if line.startswith('DO  -'):
                        lines[i] = f"DO  - {entry.doi}"
                        doi_line_exists = True
                        break
                if not doi_line_exists:
                    lines.insert(insert_point, f"DO  - {entry.doi}")
                    insert_point +=1
            
            # --- Inject or Update Attachment Path ---
            if getattr(entry, 'attachment_path', None):
                l1_line_exists = False
                for i, line in enumerate(lines):
                    if line.startswith('L1  -'):
                        lines[i] = f"L1  - {entry.attachment_path}"
                        l1_line_exists = True
                        break
                if not l1_line_exists:
                    lines.insert(insert_point, f"L1  - {entry.attachment_path}")

            # Ensure ER tag exists at the end
            if er_index == -1:
                lines.append("ER  - ")

            # Write the modified lines to the file
            f.write('\n'.join(lines))
            f.write("\n\n")


def main():
    """Enhanced main function with RIS processing and DOI retrieval
    
    This is the main entry point - handles user interaction and orchestrates everything
    """
    
    print("Academic PDF Retrieval Tool with RIS Processing")
    print("=" * 60)
    
    # Step 1: Find RIS files
    print("Step 1: Looking for RIS files in current directory...")
    ris_files = find_ris_files(".")
    
    if not ris_files:
        print("No RIS files found in current directory!")
        print("Please ensure you have .ris files in the same directory as this script.")
        return
    
    print(f"Found {len(ris_files)} RIS file(s): {', '.join(ris_files)}")
    
    # Use the first RIS file found, or let user choose if multiple
    if len(ris_files) == 1:
        selected_ris = ris_files[0]
    else:
        print("\nMultiple RIS files found:")
        for i, file in enumerate(ris_files, 1):
            print(f"{i}. {file}")
        
        try:
            choice = int(input("Select file number (or press Enter for first file): ") or "1")
            selected_ris = ris_files[choice - 1]
        except (ValueError, IndexError):
            selected_ris = ris_files[0]
    
    print(f"Processing: {selected_ris}")
    
    # Step 2: Parse RIS file
    print("\nStep 2: Parsing RIS file...")
    parser = RISParser()
    entries = parser.parse_ris_file(selected_ris)
    
    if not entries:
        print("No entries found in RIS file!")
        return
    
    print(f"Parsed {len(entries)} bibliography entries")
    
    # Step 3: Retrieve missing DOIs
    print("\nStep 3: Checking for missing DOIs and retrieving them...")
    doi_retriever = DOIRetriever()
    entries = doi_retriever.retrieve_missing_dois(entries)
    
    # Save updated RIS file with new DOIs
    updated_ris_path = "updated_bibliography.ris"
    save_updated_ris(entries, updated_ris_path)
    print(f"Saved updated bibliography with retrieved DOIs to: {updated_ris_path}")
    
    # Step 4: Configure PDF retrieval
    print("\nStep 4: Setting up PDF retrieval...")
    
    # Proxy configuration
    use_proxy = input("Do you want to use an institutional proxy? (y/n): ").lower().startswith('y')
    proxy_config = None
    if use_proxy:
        proxy_url = input("Enter your proxy URL (e.g., https://proxy.lib.university.edu/login?url=): ")
        if proxy_url:
            proxy_config = ProxyConfig(proxy_url=proxy_url)

    # Linkresolver configuration
    use_resolver = input("Do you want to use a library linkresolver? (y/n): ").lower().startswith('y')
    linkresolver_url = None
    if use_resolver:
        linkresolver_url = input("Enter your linkresolver URL (e.g., https://resolver.university.edu/openurl?): ")
    
    # Step 5: Download PDFs
    print("\nStep 5: Starting PDF download process...")
    
    retriever = EnhancedPDFRetriever(
        proxy_config=proxy_config,
        download_dir="files",
        linkresolver_url=linkresolver_url
    )
    
    # Process entries and download PDFs
    results = retriever.process_ris_entries(entries, max_retries=2, delay=2.0)
    
    # Re-save RIS including any attached local PDFs (L1 fields)
    save_updated_ris(entries, updated_ris_path)

    # Step 6: Generate reports
    print("\nStep 6: Generating results report...")
    
    report_path = "pdf_retrieval_report.txt"
    save_results_report(results, report_path)
    
    # Print summary
    print("\nResults Summary:")
    print("-" * 40)
    
    successful = sum(1 for r in results.values() if r.get('status') == 'success')
    total = len([e for e in entries if e.doi])
    
    print(f"Total entries with DOI processed: {total}")
    print(f"Successful PDF downloads: {successful}")
    print(f"Failed downloads: {total - successful}")
    if total > 0:
        print(f"Success rate: {successful/total*100:.1f}%")
    
    print(f"\nFiles created:")
    print(f"- {updated_ris_path} (RIS file with retrieved DOIs & PDF links)")
    print(f"- {report_path} (detailed results)")
    print(f"- files/ (directory with PDF files)")
    
    # Show successful downloads
    if successful > 0:
        print(f"\nSuccessfully downloaded PDFs:")
        for result in results.values():
            if result.get('status') == 'success':
                filename = Path(result.get('filepath', '')).name
                title = result.get('title', 'Unknown Title')
                print(f"- {filename}")
                print(f"  Title: {title[:60]}{'...' if len(title) > 60 else ''}")
    
    print(f"\nProcess complete!")


if __name__ == "__main__":
    main()
