#!/usr/bin/env python3
"""
PEPPOL Business Cards Synchronization Script
Streams through large XML export and splits by country
"""
import argparse
import sys
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import re
try:
    from lxml import etree as ET
except ImportError:
    sys.exit("lxml is not installed. Please run 'pip install lxml' to use this script.")
from typing import Dict, List, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError
import time
import zlib
import subprocess
import socket
import getpass


def convert_card(card_xml: str):
    """Parse one <businesscard> and return (country, regdate, entity name, crc32 of participant id, pretty bytes, error).
    Module-level so worker processes can run it."""
    try:
        root = ET.fromstring(card_xml.encode('utf-8'))
    except ET.XMLSyntaxError as e:
        return None, None, None, None, None, f"Error parsing card XML: {e} - XML: {card_xml[:200]}"
    entity = root.find(".//entity")
    country = entity.get("countrycode") if entity is not None else None
    regdate = root.find(".//regdate")
    date = None
    if regdate is not None and regdate.text:
        text = regdate.text.strip()
        if len(text) >= 10:
            date = text[:10]
    name = root.find(".//name")
    entity_name = name.get("name") if name is not None else None
    participant = root.find(".//participant")
    if participant is not None:
        participant_id = f"{participant.get('scheme', '')}::{participant.get('value', '')}"
    else:
        participant_id = card_xml
    partition_hash = zlib.crc32(participant_id.encode('utf-8'))
    # Pretty print with lxml, indented one level under <root>
    pretty = ET.tostring(root, pretty_print=True, encoding='unicode')
    card_bytes = ("\n    " + pretty.strip().replace('\n', '\n    ')).encode('utf-8')
    return country, date, entity_name, partition_hash, card_bytes, None


def convert_batch(cards):
    return [convert_card(c) for c in cards]


class PeppolSync:
    """Main class for PEPPOL export synchronization"""

    def __init__(self, tmp_dir: str = "tmp", verbose: bool = False, max_bytes: int = 1000000, keep_tmp: bool = False, jobs: int = 1):
        self.tmp_dir = Path(tmp_dir)
        self.verbose = verbose
        self.extracts_dir = Path("extracts")
        self.docs_dir = Path("docs")
        self.log_dir = Path("log")
        self.partitions: Dict[str, int] = {}     # bucket -> number of files
        self.country_last_n: Dict[str, int] = {}  # country -> N of its most recent month
        self.paths: Dict[tuple, Path] = {}        # (bucket, index) -> output path of every file written this run
        self.max_bytes = max_bytes
        self.keep_tmp = keep_tmp
        self.jobs = max(1, jobs)

        # Create directories
        self.tmp_dir.mkdir(exist_ok=True)
        self.extracts_dir.mkdir(exist_ok=True)
        self.log_dir.mkdir(exist_ok=True)

        # Statistics
        self.stats = defaultdict(int)
        self.file_count = 0  # Track number of output files created

        # Setup logging
        log_file = self.log_dir / "peppol_sync.log"
        self.log_handle = open(log_file, "w") # Changed to 'w' to start empty
        self.log(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.log(f"User: {getpass.getuser()}, Host: {socket.gethostname()}, CWD: {os.getcwd()}")

    def log(self, message: str):
        """Write to log file"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_handle.write(f"{timestamp} | {message}\n")
        self.log_handle.flush()

    def progress(self, message: str):
        """Print progress message"""
        if not self.verbose:
            print(f"\r... {message}", end="", flush=True)
        else:
            print(f"... {message}")

    def success(self, message: str):
        """Print success message"""
        print(f"\n✅  {message}")

    def announce(self, message: str):
        """Print announcement"""
        print(f"⏳  {message}")

    def download_xml(self, force: bool = False) -> Path:
        """Download PEPPOL XML export if needed"""
        url = "https://directory.peppol.eu/export/businesscards"
        output_file = self.tmp_dir / "directory-export-business-cards.xml"

        # Skip if file exists and not forcing
        if output_file.exists() and not force:
            file_size_mb = output_file.stat().st_size / (1024 * 1024)
            self.log(f"Using existing file: {output_file} ({file_size_mb:.1f} MB)")
            return output_file

        self.announce(f"Downloading PEPPOL export from {url}")
        self.log(f"download_xml: {url}")

        start_time = time.time() # Record start time

        try:
            # Open URL connection.
            # urllib sends "Accept-Encoding: identity" by default, which directory.peppol.eu
            # rejects with HTTP 406 since Sept 2026. Advertise gzip instead and decompress
            # on the fly if the server actually compresses the response.
            request = Request(url, headers={
                "User-Agent": "per_country (https://github.com/peppoller/per_country)",
                "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate",
            })
            with urlopen(request) as response:
                # Download in chunks
                chunk_size = 8192  # 8KB chunks
                downloaded = 0
                encoding = (response.headers.get("Content-Encoding") or "").lower()
                decompressor = None
                if encoding == "gzip":
                    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
                elif encoding == "deflate":
                    decompressor = zlib.decompressobj()
                self.log(f"download_xml: HTTP {response.status}, Content-Encoding: {encoding or 'none'}")

                with open(output_file, 'wb') as f:
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            if decompressor:
                                f.write(decompressor.flush())
                            break

                        if decompressor:
                            chunk = decompressor.decompress(chunk)
                        f.write(chunk)
                        downloaded += len(chunk)

                        # Update progress every MB
                        if downloaded % (100 * 1024 * 1024) == 0 or not chunk:
                            duration = time.time() - start_time
                            downloaded_mb = downloaded / (1024 * 1024)
                            throughput = downloaded_mb / duration if duration > 0 else 0
                            self.progress(f"Downloading {downloaded_mb:.1f} MB @ {duration:.1f}s: {throughput:.2f} MB/s")

            end_time = time.time() # Record end time

            # Verify file was created
            if output_file.exists():
                file_size_mb = output_file.stat().st_size / (1024 * 1024)
                duration = end_time - start_time
                throughput = file_size_mb / duration if duration > 0 else 0
                self.success(f"Downloaded to {output_file.name} ({file_size_mb:.0f} MB) in {duration:.0f}s at {throughput:.0f} MB/s")
                self.log(f"download_xml: {file_size_mb:.0f} MB downloaded in {duration:.0f}s at {throughput:.0f} MB/s")
                return output_file
            else:
                raise FileNotFoundError(f"Download completed but file not found: {output_file}")

        except URLError as e:
            error_msg = f"Failed to download from {url}: {e}"
            self.log(f"download_xml error: {error_msg}")
            raise Exception(error_msg)




    def bucket_file(self, bucket: str, index: int) -> Path:
        """Path of file `index` (1-based) of a country/month bucket, e.g. extracts/BE/2026-08/business-cards.000003.xml"""
        country, month = bucket.split("/", 1)
        return self.extracts_dir / country / month / f"business-cards.{index:06d}.xml"

    def plan_partitions(self):
        """Decide how many files each country/month bucket gets, from the previous run's extracts.

        Cards are assigned to a file by hash of their participant id, so a bucket's file
        count N must be stable across runs: any change reshuffles the whole bucket. N is
        read back from the highest file index on disk and only doubled when files average
        more than 1.5x max_bytes, or halved when they average less than 0.35x, which keeps
        the average within [0.7x, 1.5x] without flip-flopping near a boundary.
        A bucket seen for the first time starts with the N of that country's latest month.
        Must run before cleanup_extracts()."""
        rescaled = 0
        if not self.extracts_dir.is_dir():
            return
        for country_dir in sorted(self.extracts_dir.iterdir()):
            if not country_dir.is_dir():
                continue
            for month_dir in sorted(country_dir.iterdir()):
                files = list(month_dir.glob("business-cards.*.xml")) if month_dir.is_dir() else []
                if not files:
                    continue
                n_prev = max(int(f.stem.rsplit(".", 1)[-1]) for f in files)
                size = sum(f.stat().st_size for f in files)
                n = max(1, n_prev)
                while size / n > 1.5 * self.max_bytes:
                    n *= 2
                while n > 1 and size / n < 0.35 * self.max_bytes:
                    n //= 2
                bucket = f"{country_dir.name}/{month_dir.name}"
                self.partitions[bucket] = n
                if n != n_prev:
                    rescaled += 1
                    self.log(f"Partition {bucket}: {n_prev} -> {n} files ({size / (1024 * 1024):.1f} MB)")
                if month_dir.name[:2] != "00":  # skip the no-date placeholders when picking "latest month"
                    self.country_last_n[country_dir.name] = n
        self.log(f"Partition plan: {len(self.partitions)} buckets from previous extracts, {rescaled} rescaled")
        self.announce(f"Partition plan: {len(self.partitions)} buckets, {rescaled} rescaled")

    def extract_date_from_etree(self, element: ET.Element) -> Optional[str]:
        """Extract registration date from ElementTree element"""
        regdate = element.find(".//regdate")
        if regdate is not None and regdate.text:
            date_str = regdate.text.strip()
            if len(date_str) >= 10:
                return date_str[:10]
        return None

    def extract_entity_name_from_etree(self, element: ET.Element) -> Optional[str]:
        """Extract entity name from ElementTree element"""
        name = element.find(".//name")
        if name is not None:
            return name.get("name")
        return None

    def iter_cards(self, f, chunk_size: int = 1024 * 1024):
        """Yield (header, None) once, then (None, card_xml) for every <businesscard>.
        Scans a rolling buffer by index; the buffer is only re-sliced when a chunk is appended."""
        separator = "</businesscard>"
        buffer = ""
        while "<businesscard>" not in buffer:
            chunk = f.read(chunk_size)
            if not chunk:
                return
            buffer += chunk
        header_end = buffer.find("<businesscard>")
        yield buffer[:header_end], None
        pos = header_end
        while True:
            sep_index = buffer.find(separator, pos)
            if sep_index < 0:
                chunk = f.read(chunk_size)
                if not chunk:
                    return
                buffer = buffer[pos:] + chunk
                pos = 0
                continue
            end_index = sep_index + len(separator)
            yield None, buffer[pos:end_index]
            pos = end_index

    def iter_converted(self, cards):
        """Yield convert_card() results in input order, using a worker pool when jobs > 1.
        Prefetch is bounded so the input file is never read far ahead of the writer."""
        if self.jobs <= 1:
            for card_xml in cards:
                yield convert_card(card_xml)
            return

        import multiprocessing as mp
        from collections import deque
        try:
            ctx = mp.get_context("fork")
        except ValueError:
            ctx = mp.get_context()
        batch_size = 2000
        max_pending = self.jobs * 4
        pending = deque()
        with ctx.Pool(self.jobs) as pool:
            batch = []
            for card_xml in cards:
                batch.append(card_xml)
                if len(batch) >= batch_size:
                    pending.append(pool.apply_async(convert_batch, (batch,)))
                    batch = []
                    if len(pending) >= max_pending:
                        yield from pending.popleft().get()
            if batch:
                pending.append(pool.apply_async(convert_batch, (batch,)))
            while pending:
                yield from pending.popleft().get()

    def process_xml(self, input_file: Path):
        """Process XML file using text splitting for performance"""
        self.announce(f"Processing {input_file.name} with text splitting ({self.jobs} worker(s))")
        self.log(f"Starting text processing: {input_file} with {self.jobs} worker(s)")

        if not input_file.exists():
            raise FileNotFoundError(f"Input file not found: {input_file}")

        start_time = time.time()  # Record start time

        processed_cards = 0
        final_footer = b"\n</root>"
        # Output is buffered per file in memory and flushed in large chunks with a
        # short-lived open/write/close. Keeping thousands of files open and feeding
        # each one 2KB appends was measurably slower, and needed no fd-limit juggling.
        flush_bytes = 128 * 1024
        pending: Dict[tuple, List[bytes]] = {}
        pending_size: Dict[tuple, int] = {}

        def flush(key: tuple, tail: bytes = b""):
            chunks = pending[key]
            data = b"".join(chunks) + tail
            chunks.clear()
            pending_size[key] = 0
            path = self.paths.get(key)
            if path is None:
                path = self.bucket_file(*key)
                self.paths[key] = path
                path.parent.mkdir(parents=True, exist_ok=True)
                self.file_count += 1
                with open(path, "wb") as handle:
                    handle.write(header_bytes + data)
            else:
                with open(path, "ab") as handle:
                    handle.write(data)

        try:
            with open(input_file, 'r', encoding='utf-8') as f:
                items = self.iter_cards(f)
                try:
                    header, _ = next(items)
                except StopIteration:
                    self.log("No <businesscard> tag found.")
                    return 0

                # Remove creationdt from header to make it static
                header = re.sub(r'creationdt="[^"]*"', '', header)
                header_bytes = header.replace('><', '>\n<').encode('utf-8')

                cards = (card_xml for _, card_xml in items)
                for country, date, entity_name, partition_hash, card_bytes, error in self.iter_converted(cards):
                    processed_cards += 1
                    if processed_cards % 100000 == 0:
                        duration = time.time() - start_time
                        throughput = processed_cards / duration if duration > 0 else 0
                        self.progress(
                            f"{processed_cards:,} business cards in {duration:.1f}s: {throughput:.0f} cards/sec")

                    if error:
                        self.log(error)
                        continue
                    if not country:
                        self.log(f"Could not extract country from card: {card_bytes[:100]!r}")
                        continue

                    self.stats[f"country_{country}"] += 1

                    # Bucket by registration month; cards without regdate go to 0000-00
                    month = date[:7] if date else "0000-00"

                    if not date:
                        safe_name = "".join(filter(str.isalnum, entity_name or ""))[:5].upper()
                        date = f"2000-{safe_name}" if safe_name else "2000-UNKNOWN"

                    self.stats[f"date_{date}"] += 1

                    # File writing logic: stable partition. A card always lands in the
                    # same file of its country/month bucket (hash of participant id mod N),
                    # so a changed card touches one file instead of shifting all later ones.
                    bucket = f"{country}/{month}"
                    n = self.partitions.get(bucket)
                    if n is None:
                        n = self.country_last_n.get(country, 1)
                        self.partitions[bucket] = n
                        self.log(f"Partition {bucket}: new bucket, {n} files")
                    key = (bucket, partition_hash % n + 1)
                    chunks = pending.get(key)
                    if chunks is None:
                        pending[key] = chunks = []
                        pending_size[key] = 0
                    chunks.append(card_bytes)
                    pending_size[key] += len(card_bytes)
                    if pending_size[key] >= flush_bytes:
                        flush(key)
        finally:
            # Write out whatever is still buffered, closing every file with its footer
            for key in list(pending):
                flush(key, final_footer)

        duration = time.time() - start_time
        throughput = processed_cards / duration if duration > 0 else 0
        self.success(f"Processed {processed_cards:,} business cards in {duration:.0f}s: {throughput:.0f} cards/sec")
        self.log(f"Processed {processed_cards:,} business cards in {duration:.0f}s: {throughput:.0f} cards/sec")

        return processed_cards

    def generate_report(self):
        """Generate a markdown report of the sync operation"""
        report_path = self.docs_dir / "report.md"
        self.announce(f"Generating report: {report_path}")

        with open(report_path, "w", encoding="utf-8") as f:
            f.write("# PEPPOL Sync Report\n\n")
            f.write(f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            f.write("| Country | Months | Files | Cards | Size (MB) |\n")
            f.write("|---|---:|---:|---:|---:|\n")

            total_months = 0
            total_files = 0
            total_cards = 0
            total_size_mb = 0

            countries = sorted([k.replace("country_", "") for k in self.stats.keys() if k.startswith("country_")])

            for country in countries:
                country_dir = self.extracts_dir / country
                if not country_dir.is_dir():
                    continue

                files = list(country_dir.glob("**/*.xml"))
                month_count = len({p.parent for p in files})
                file_count = len(files)
                card_count = self.stats.get(f"country_{country}", 0)
                size_bytes = sum(p.stat().st_size for p in files)
                size_mb = size_bytes / (1024 * 1024)

                f.write(f"| {country} | {month_count} | {file_count} | {card_count} | {size_mb:.2f} |\n")

                total_months += month_count
                total_files += file_count
                total_cards += card_count
                total_size_mb += size_mb

            f.write(f"| **Total** | **{total_months}** | **{total_files}** | **{total_cards}** | **{total_size_mb:.2f}** |\n")

        self.success(f"Report generated at {report_path}")
        self.log(f"Report generated at {report_path}")

    def cleanup_extracts(self):
        """Delete all existing XML files in the extracts directory"""
        self.announce("Cleaning up existing extracts")
        deleted_files = 0
        for file_path in self.extracts_dir.glob("**/*.xml"):
            if file_path.is_file():
                file_path.unlink()
                deleted_files += 1
        # Remove directories left empty (deepest first)
        for dir_path in sorted(self.extracts_dir.glob("**/"), key=lambda p: len(p.parts), reverse=True):
            if dir_path != self.extracts_dir and dir_path.is_dir() and not any(dir_path.iterdir()):
                dir_path.rmdir()
        self.success(f"Deleted {deleted_files} XML files from {self.extracts_dir}/")
        self.log(f"Deleted {deleted_files} XML files from {self.extracts_dir}/")

    def sync(self, force_download: bool = False, cleanup: bool = False):
        """Main sync operation"""
        self.log("Starting sync operation")

        # Read the file count per bucket from the previous run before anything is deleted
        self.plan_partitions()

        if cleanup:
            self.cleanup_extracts()

        self.announce(f"Max bytes per file: {self.max_bytes:,}")

        # Download XML file if needed
        try:
            input_file = self.download_xml(force=force_download)
        except Exception as e:
            print(f"❌ Download failed: {e}")
            return 1

        # Show file size
        file_size_mb = input_file.stat().st_size / (1024 * 1024)
        self.announce(f"Processing file: {input_file.name} ({file_size_mb:.1f} MB)")

        # Process XML
        try:
            cards_processed = self.process_xml(input_file)

            # Show summary
            print("\n📊 Summary:")
            print(f"   Total business cards: {cards_processed:,}")
            
            countries = [k.replace("country_", "") for k in self.stats.keys() if k.startswith("country_")]
            print(f"   Countries found: {len(countries)}")
            self.log(f"Countries found: {len(countries)}")

            print(f"   Output files created: {self.file_count}")
            self.log(f"Output files created: {self.file_count}")
            print(f"   Output directory: {self.extracts_dir}/")

            self.success("Sync complete!")
            self.generate_report()
            return 0

        except Exception as e:
            print(f"\n❌ Error: {e}")
            self.log(f"Error: {e}")
            return 1

        finally:
            self.log_handle.close()

    def cleanup_after(self):
        """Close any open resources and clean up temp files"""
        # Close log file
        if self.log_handle and not self.log_handle.closed:
            self.log_handle.close()

        # Clean up tmp files unless keep_tmp is set
        if not self.keep_tmp and self.tmp_dir.exists():
            import shutil
            try:
                files_removed = 0
                for file_path in self.tmp_dir.glob("*"):
                    if file_path.is_file():
                        file_path.unlink()
                        files_removed += 1

                if files_removed > 0:
                    print(f"\n🧹 Cleaned up {files_removed} temporary file(s) from {self.tmp_dir}/")
            except Exception as e:
                print(f"\n⚠️  Warning: Could not clean up tmp files: {e}")

    def show_huge_files(self, number: int = 10) -> int:
        """Show the N largest XML files under extracts/"""
        self.announce(f"Finding the {number} largest XML files under {self.extracts_dir}/")
        command = f"find {self.extracts_dir} -name \"*.xml\" -type f -exec du -h {{}} + | sort -rh | head -n {number}"
        
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, check=True)
            print(result.stdout)
            self.success(f"Displayed {number} largest files.")
            return 0
        except subprocess.CalledProcessError as e:
            print(f"❌ Error executing command: {e}")
            print(f"Stderr: {e.stderr}")
            self.log(f"Error in show_huge_files: {e.stderr}")
            return 1


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Synchronize PEPPOL export into git-managed files",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "action",
        choices=["sync", "check", "download", "huge"],
        help="Action to perform"
    )

    parser.add_argument(
        "-V", "--verbose",
        action="store_true",
        help="Enable verbose output"
    )

    parser.add_argument(
        "-F", "--force",
        action="store_true",
        help="Force re-download of XML file even if it exists"
    )

    parser.add_argument(
        "-C", "--nocleanup",
        action="store_true",
        help="Do not delete existing XML files in extracts/ before starting (default: delete)"
    )

    parser.add_argument(
        "-K" ,"--keep-tmp",
        action="store_true",
        help="Keep temporary files after processing (default: delete)"
    )

    parser.add_argument(
        "-T", "--tmp",
        default="tmp",
        help="Temporary directory (default: tmp)"
    )

    parser.add_argument(
        "-M", "--max",
        type=int,
        default=2000000,
        help="Maximum number of bytes per output file (default: 2000000)"
    )

    parser.add_argument(
        "-j", "--jobs",
        type=int,
        default=max(1, (os.cpu_count() or 2) - 1),
        help="Worker processes for XML parsing (default: CPU count - 1; 1 = no worker pool)"
    )

    args = parser.parse_args()

    # Create sync instance
    syncer = PeppolSync(
        tmp_dir=args.tmp,
        verbose=args.verbose,
        max_bytes=args.max,
        keep_tmp=args.keep_tmp,
        jobs=args.jobs
    )

    try:
        if args.action == "sync":
            return syncer.sync(force_download=args.force, cleanup=not args.nocleanup)
        elif args.action == "download":
            try:
                input_file = syncer.download_xml(force=args.force)
                file_size_mb = input_file.stat().st_size / (1024 * 1024)
                print(f"\n📁 Downloaded file:")
                print(f"   Location: {input_file}")
                print(f"   Size: {file_size_mb:.1f} MB")
                return 0
            except Exception as e:
                print(f"\n❌ Download failed: {e}")
                return 1
        elif args.action == "check":
            print("✅ Configuration OK")
            print(f"   Temp directory: {syncer.tmp_dir}")
            print(f"   Extracts directory: {syncer.extracts_dir}")
            return 0
        elif args.action == "huge":
            return syncer.show_huge_files(10)
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        return 130
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        return 1
    finally:
        syncer.cleanup_after()


if __name__ == "__main__":
    sys.exit(main())
