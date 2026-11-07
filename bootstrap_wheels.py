#!/usr/bin/env python3
"""
Bootstrap bundled Python dependencies from wheels/ into _vendor/
Works completely offline without pip or root privileges.
"""
import glob
import os
import sys
import time
import zipfile

def bootstrap():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    wheels_dir = os.path.join(base_dir, "wheels")
    vendor_dir = os.path.join(base_dir, "_vendor")

    if not os.path.isdir(wheels_dir):
        return

    py_minor = sys.version_info.minor
    marker_file = os.path.join(vendor_dir, f".extracted_py3_{py_minor}")

    if os.path.exists(marker_file) and os.path.isdir(vendor_dir):
        return

    t0 = time.time()
    os.makedirs(vendor_dir, exist_ok=True)

    v_tag = f"cp3{py_minor}"
    whl_files = sorted(glob.glob(os.path.join(wheels_dir, "*.whl")))
    extracted_count = 0

    for whl in whl_files:
        fname = os.path.basename(whl)
        # Check compatibility
        is_pure = "none-any" in fname
        is_abi3 = ("abi3" in fname or "cp310-abi3" in fname) and sys.version_info >= (3, 10)
        is_ver_match = v_tag in fname

        if is_pure or is_abi3 or is_ver_match:
            try:
                with zipfile.ZipFile(whl, "r") as zf:
                    zf.extractall(vendor_dir)
                extracted_count += 1
            except Exception as e:
                print(f"[SchoolHub Bootstrap] Warning extracting {fname}: {e}", file=sys.stderr)

    try:
        with open(marker_file, "w", encoding="utf-8") as f:
            f.write(f"extracted {extracted_count} wheels at {time.ctime()}\n")
    except Exception:
        pass

    duration = time.time() - t0
    print(f"[SchoolHub Bootstrap] Unpacked {extracted_count} packages into _vendor for Python 3.{py_minor} in {duration:.2f}s", flush=True)

if __name__ == "__main__":
    bootstrap()
