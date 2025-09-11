#!/usr/bin/env python3
"""
GUI for retriever.py with collapsible log and progress bar

TODO: make this look prettier someday - the UI is kinda ugly rn
v2 - added progress bar (finally!)
v3 - fixed the annoying log spam issue
"""

import threading
import queue
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
import logging

# import our main retrieval classes
# if this fails, we'll show an error but the GUI will still run (sorta)
try:
    from retriever import (
        RISParser,
        DOIRetriever,
        EnhancedPDFRetriever,
        ProxyConfig,
        save_updated_ris,
        save_results_report,
    )
except ImportError:
    messagebox.showerror(
        "Import Error",
        "Could not find retriever.py. Please ensure it is in the same directory.",
    )
    # dummy classes so the GUI doesn't crash - hacky but works
    class RISParser:
        def parse_ris_file(self, path): return []
    class DOIRetriever:
        def retrieve_missing_dois(self, entries): return entries
    class EnhancedPDFRetriever:
        def __init__(self, **kwargs): pass
        def process_ris_entries(self, entries, **kwargs):
            return {e: {"status": "success"} for e in entries}
    class ProxyConfig:
        def __init__(self, **kwargs): pass
    def save_updated_ris(entries, path): pass
    def save_results_report(results, path): pass


class CollapsibleSection(ttk.Frame):
    """Collapsible section for log - found this pattern on stackoverflow"""
    def __init__(self, parent, title="Section"):
        super().__init__(parent)
        self._expanded = tk.BooleanVar(value=False)

        self.toggle_btn = ttk.Checkbutton(
            self, text=title, style="Toolbutton",
            variable=self._expanded, command=self._toggle
        )
        self.toggle_btn.pack(fill="x", pady=2)

        self.sub_frame = ttk.Frame(self)

    def _toggle(self):
        if self._expanded.get():
            self.sub_frame.pack(fill="both", expand=True)
        else:
            self.sub_frame.forget()


class TkLogHandler(logging.Handler):
    """custom log handler that sends messages to a queue for GUI display"""
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self.log_queue.put(msg)
        except Exception:
            pass  # ignore errors in logging - we don't want this to crash the app


class RetrieverGUI(tk.Tk):
    """main GUI class for the PDF retriever
    
    handles all the tkinter stuff and coordinates with the backend classes
    """
    def __init__(self):
        super().__init__()
        self.title("Academic PDF Retriever")
        self.geometry("820x650")

        # State vars
        self.selected_ris_path: Path | None = None
        self.worker_thread: threading.Thread | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        # setup Logger - this was a pain to get working properly
        self.logger = logging.getLogger("retriever_gui")
        self.logger.setLevel(logging.INFO)
        handler = TkLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        self._detach_existing_gui_handlers(logging.getLogger())
        logging.getLogger().addHandler(handler)

        self._build_ui()
        self._poll_log_queue()

    def _detach_existing_gui_handlers(self, root_logger):
        """remove any existing GUI handlers to avoid duplicates"""
        to_remove = [h for h in root_logger.handlers if isinstance(h, TkLogHandler)]
        for h in to_remove:
            root_logger.removeHandler(h)

    def _build_ui(self):
        """build the main UI - lots of boilerplate tkinter code here"""
        pad = 8
        frm = ttk.Frame(self)
        frm.pack(fill=tk.BOTH, expand=True, padx=pad, pady=pad)

        # RIS file selection row
        ris_row = ttk.Frame(frm)
        ris_row.pack(fill=tk.X, pady=(0, pad))
        ttk.Label(ris_row, text="RIS file:").pack(side=tk.LEFT)
        self.ris_var = tk.StringVar()
        self.ris_entry = ttk.Entry(ris_row, textvariable=self.ris_var)
        self.ris_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(pad, pad))
        ttk.Button(ris_row, text="Browse...", command=self._choose_ris).pack(side=tk.LEFT)

        # Options section
        opt_frame = ttk.LabelFrame(frm, text="Options")
        opt_frame.pack(fill=tk.X, pady=(0, pad))

        # DOI retrieval options
        doi_opts_frame = ttk.Frame(opt_frame)
        doi_opts_frame.pack(fill=tk.X, padx=pad, pady=(pad, 0))
        self.chk_doi_var = tk.BooleanVar(value=True)
        self.doi_checkbox = ttk.Checkbutton(
            doi_opts_frame,
            text="Retrieve missing DOIs before PDF download",
            variable=self.chk_doi_var
        )
        self.doi_checkbox.pack(anchor=tk.W)

        self.chk_doi_only_var = tk.BooleanVar(value=False)
        self.doi_only_checkbox = ttk.Checkbutton(
            doi_opts_frame,
            text="DOI Retrieval Only (no PDF download)",
            variable=self.chk_doi_only_var,
            command=self._on_toggle_doi_only
        )
        self.doi_only_checkbox.pack(anchor=tk.W, pady=(0, pad))

        # Proxy settings
        self.chk_proxy_var = tk.BooleanVar(value=False)
        proxy_checkbox = ttk.Checkbutton(
            opt_frame, text="Use Institutional Proxy", variable=self.chk_proxy_var,
            command=self._toggle_proxy_entry
        )
        proxy_checkbox.pack(anchor=tk.W, padx=pad, pady=pad)

        proxy_row = ttk.Frame(opt_frame)
        proxy_row.pack(fill=tk.X, padx=pad, pady=(0, pad))
        ttk.Label(proxy_row, text="Proxy URL:").pack(side=tk.LEFT)
        self.proxy_var = tk.StringVar(value="http://proxy.library.upei.ca/login?qurl=%u")  # default to UPEI proxy
        self.proxy_entry = ttk.Entry(proxy_row, textvariable=self.proxy_var, state=tk.DISABLED)
        self.proxy_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(pad, 0))

        # Linkresolver settings - added this in march 2024
        self.chk_resolver_var = tk.BooleanVar(value=False)
        resolver_checkbox = ttk.Checkbutton(
            opt_frame, text="Use Library Linkresolver (fallback)", variable=self.chk_resolver_var,
            command=self._toggle_resolver_entry
        )
        resolver_checkbox.pack(anchor=tk.W, padx=pad, pady=pad)

        resolver_row = ttk.Frame(opt_frame)
        resolver_row.pack(fill=tk.X, padx=pad, pady=(0, pad))
        ttk.Label(resolver_row, text="Resolver URL:").pack(side=tk.LEFT)
        self.resolver_var = tk.StringVar(value="https://resolver-ebscohost-com.proxy.library.upei.ca/openurl?")  # upei default
        self.resolver_entry = ttk.Entry(resolver_row, textvariable=self.resolver_var, state=tk.DISABLED)
        self.resolver_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(pad, 0))

        # Action buttons
        action_row = ttk.Frame(frm)
        action_row.pack(fill=tk.X, pady=(pad, pad))
        self.start_btn = ttk.Button(action_row, text="Start", command=self._on_start)
        self.start_btn.pack(side=tk.LEFT)
        self.cancel_btn = ttk.Button(action_row, text="Close", command=self.destroy)
        self.cancel_btn.pack(side=tk.RIGHT)

        # Collapsible log section - this was tricky to get right
        log_section = CollapsibleSection(frm, title="Log")
        log_section.pack(fill="both", expand=True, pady=(0, pad))
        self.log_text = ScrolledText(log_section.sub_frame, height=20, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Progress bar at the bottom
        self.progress_var = tk.IntVar(value=0)
        self.progressbar = ttk.Progressbar(
            self, orient="horizontal", length=100, mode="determinate", variable=self.progress_var
        )
        self.progressbar.pack(fill=tk.X, side=tk.BOTTOM)

        # Status bar
        self.status_var = tk.StringVar(value="Idle")
        status = ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status.pack(fill=tk.X, side=tk.BOTTOM)

    def _toggle_proxy_entry(self):
        """enable/disable proxy entry field based on checkbox"""
        self.proxy_entry.config(state=tk.NORMAL if self.chk_proxy_var.get() else tk.DISABLED)

    def _toggle_resolver_entry(self):
        """enable/disable resolver entry field based on checkbox"""
        self.resolver_entry.config(state=tk.NORMAL if self.chk_resolver_var.get() else tk.DISABLED)

    def _on_toggle_doi_only(self):
        """when DOI-only is checked, force DOI retrieval to be enabled"""
        if self.chk_doi_only_var.get():
            self.chk_doi_var.set(True)
            self.doi_checkbox.config(state=tk.DISABLED)
        else:
            self.doi_checkbox.config(state=tk.NORMAL)

    def _choose_ris(self):
        """open file dialog to select RIS file"""
        path = filedialog.askopenfilename(title="Select RIS file",
                                          filetypes=[("RIS files", "*.ris *.RIS"), ("All files", "*.*")])
        if path:
            self.selected_ris_path = Path(path)
            self.ris_var.set(str(path))

    def _append_log(self, text):
        """add text to the log widget - this was annoying to get right with the scrolling"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)  # auto-scroll to bottom
        self.log_text.config(state=tk.DISABLED)

    def _poll_log_queue(self):
        """check for new log messages and add them to the display
        
        this runs continuously to update the log in real-time
        """
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass  # no messages, that's fine
        self.after(150, self._poll_log_queue)  # check again in 150ms

    def _on_start(self):
        """start the PDF retrieval process - runs in a separate thread"""
        # basic validation first
        if not self.ris_var.get().strip():
            messagebox.showwarning("Missing RIS", "Please select an RIS file.")
            return
        self.selected_ris_path = Path(self.ris_var.get())
        if not self.selected_ris_path.exists():
            messagebox.showerror("File not found", f"RIS file not found:\n{self.selected_ris_path}")
            return

        # disable start button while running
        self.start_btn.config(state=tk.DISABLED)
        self.status_var.set("Running...")
        
        # clear the log
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)
        self.progress_var.set(0)
        self._append_log("Starting...")

        # get settings from UI
        do_doi = self.chk_doi_var.get()
        doi_only = self.chk_doi_only_var.get()
        proxy_url = self.proxy_var.get().strip() if self.chk_proxy_var.get() else ""
        resolver_url = self.resolver_var.get().strip() if self.chk_resolver_var.get() else ""

        # start worker thread - don't want to block the GUI
        self.worker_thread = threading.Thread(
            target=self._run_workflow,
            args=(self.selected_ris_path, do_doi, proxy_url, doi_only, resolver_url),
            daemon=True
        )
        self.worker_thread.start()

    def _run_workflow(self, ris_path, do_doi, proxy_url, doi_only, resolver_url):
        """the main workflow - runs in background thread
        
        this does all the heavy lifting: parsing RIS, retrieving DOIs, downloading PDFs
        """
        try:
            # step 1: parse RIS file
            parser = RISParser()
            entries = parser.parse_ris_file(str(ris_path))
            if not entries:
                raise RuntimeError("No entries parsed from RIS file.")

            total_with_doi = 0
            completed = 0

            # generate output filename
            updated_ris_name = f"updated_{ris_path.name}"
            updated_ris_path = ris_path.parent / updated_ris_name

            # step 2: retrieve missing DOIs if requested
            if do_doi:
                doi_ret = DOIRetriever()
                entries = doi_ret.retrieve_missing_dois(entries)
                save_updated_ris(entries, str(updated_ris_path))
                logging.info(f"Saved {updated_ris_name} with any new DOIs.")

            # if DOI-only mode, we're done
            if doi_only:
                logging.info("DOI retrieval only complete. All done.")
                self.status_var.set("DOI retrieval complete.")
                return

            # step 3: download PDFs
            entries_to_process = [e for e in entries if e.doi]
            total_with_doi = len(entries_to_process)
            self.progressbar["maximum"] = total_with_doi

            # setup PDF retriever
            proxy_cfg = ProxyConfig(proxy_url=proxy_url) if proxy_url else None
            retriever = EnhancedPDFRetriever(
                proxy_config=proxy_cfg,
                download_dir="downloaded_pdfs",
                linkresolver_url=resolver_url if resolver_url else None
            )

            # process each entry one by one to update progress
            results = {}
            for entry in entries_to_process:
                # process_ris_entries is designed to take a list, but we send one at a time for progress updates
                res = retriever.process_ris_entries([entry], max_retries=2, delay=2.0)
                results.update(res)
                completed += 1
                self.progress_var.set(completed)
                self.status_var.set(f"Processed {completed}/{total_with_doi}")
            
            # save final results
            save_updated_ris(entries, str(updated_ris_path))
            save_results_report(results, str(ris_path.parent / "pdf_retrieval_report.txt"))

            # calculate success rate
            successful_downloads = sum(1 for r in results.values() if r.get('status') == 'success')
            summary_msg = f"Finished. Successfully downloaded {successful_downloads} of {total_with_doi} PDFs."
            
            # log summary
            logging.info("-" * 40)
            logging.info("SUMMARY:")
            logging.info(f"PDF downloads complete. {completed}/{total_with_doi} entries with DOIs processed.")
            logging.info(f"Successful downloads: {successful_downloads}")
            logging.info(f"Failed downloads: {total_with_doi - successful_downloads}")
            logging.info("All done.")
            
            self.status_var.set(summary_msg)

        except Exception as e:
            logging.exception("An error occurred during workflow.")
            messagebox.showerror("Error", f"An unexpected error occurred:\n{e}")
            self.status_var.set("Finished with error.")
        finally:
            # re-enable start button
            self.start_btn.config(state=tk.NORMAL)
            self.status_var.set("Idle")


def main():
    """main entry point for the GUI"""
    app = RetrieverGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
