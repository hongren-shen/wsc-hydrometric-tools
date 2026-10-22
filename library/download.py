# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hongren Shen
"""Download one station's compressed WSC unit-value files, sequentially."""

import lzma
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

BASE_URL = "https://collaboration.cmc.ec.gc.ca/cmc/hydrometrics/www/UnitValueData/"


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names = set()

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href", "")
            self.names.add(Path(unquote(urlparse(href).path)).name)


def _open(url):
    return urlopen(Request(url, headers={"User-Agent": "wsc-hydrometric-tools"}), timeout=60)


def _check_xz(path):
    """Read through the compressed stream so truncated downloads are rejected."""
    with lzma.open(path, "rb") as source:
        while source.read(1024 * 1024):
            pass


def download_station(station, output_dir, data_type="Discharge", quality="corrected",
                     overwrite=False):
    """Discover available file segments and download them in serial order."""
    station = station.strip().upper()
    if not re.fullmatch(r"\d{2}[A-Z0-9]{5}", station):
        raise ValueError("Expected a WSC station number such as 02GB001.")
    if data_type not in {"Discharge", "Stage"} or quality not in {"corrected", "raw"}:
        raise ValueError("Use Discharge/Stage and corrected/raw.")
    directory = f"{BASE_URL}{data_type}/{quality}/{station[:2]}/"
    parser = _Links()
    with _open(directory) as response:
        parser.feed(response.read().decode("utf-8"))
    pattern = re.compile(
        rf"{data_type}\.Working@{station}\.\d{{8}}_{quality}\.csv\.xz"
    )
    names = sorted(name for name in parser.names if pattern.fullmatch(name))
    if not names:
        raise FileNotFoundError(f"No {data_type}/{quality} files listed for {station}.")

    folder = Path(output_dir) / data_type / quality
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in names:
        path = folder / name
        if path.exists() and not overwrite:
            _check_xz(path)
            print(f"Already downloaded: {path}")
        else:
            temporary = path.with_name(path.name + ".part")
            try:
                print(f"Downloading: {name}")
                with _open(directory + quote(name)) as source, temporary.open("wb") as target:
                    shutil.copyfileobj(source, target)
                _check_xz(temporary)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        paths.append(path)
    return paths
