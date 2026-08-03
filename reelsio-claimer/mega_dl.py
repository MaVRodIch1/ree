"""
Minimal downloader for public MEGA folder links (https://mega.nz/folder/ID#KEY).

mega.py can't do folder links (and pins an ancient pathlib backport that breaks
modern Python), so this implements just the public-folder subset of the MEGA
API: list the folder, decrypt node keys/attributes, fetch and AES-CTR decrypt
each file. Only stdlib + pycryptodome.
"""
import base64
import json
import re
import struct
from pathlib import Path

import requests
from Crypto.Cipher import AES
from Crypto.Util import Counter

API = "https://g.api.mega.co.nz/cs"


def _b64_decode(data):
    data += "==" [(2 - len(data) * 3) % 4:]
    return base64.b64decode(data.replace("-", "+").replace("_", "/").replace(",", ""))


def _str_to_a32(b):
    if isinstance(b, str):
        b = b.encode()
    if len(b) % 4:
        b += b"\0" * (4 - len(b) % 4)
    return struct.unpack(">%dI" % (len(b) // 4), b)


def _a32_to_str(a):
    return struct.pack(">%dI" % len(a), *a)


def _b64_to_a32(s):
    return _str_to_a32(_b64_decode(s))


def _aes_ecb_decrypt(data, key):
    return AES.new(key, AES.MODE_CBC, b"\0" * 16).decrypt(data)


def _decrypt_key(a, key):
    """Decrypt a node key (a32) with the shared folder key."""
    out = ()
    for i in range(0, len(a), 4):
        out += _str_to_a32(_aes_ecb_decrypt(_a32_to_str(a[i:i + 4]), _a32_to_str(key)))
    return out


def _decrypt_attr(attr, key):
    try:
        raw = _aes_ecb_decrypt(attr, _a32_to_str(key)).decode("utf-8").rstrip("\0")
        return json.loads(raw[4:]) if raw.startswith('MEGA{"') else {}
    except Exception:
        return {}


def parse_folder_link(url):
    """-> (folder_id, folder_key) or (None, None)."""
    m = re.search(r"mega\.nz/(?:#F!|folder/)([^!#?/]+)[!#]([^!#?/\s]+)", url)
    return (m.group(1), m.group(2)) if m else (None, None)


def _api(folder_id, payload, session):
    r = session.post(API, params={"id": 0, "n": folder_id},
                     data=json.dumps([payload]), timeout=60)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, int):
        raise RuntimeError(f"MEGA API error {data}")
    return data[0]


def list_folder(url, session=None):
    """List files in a public folder: [{name, node, key, iv, size}]."""
    folder_id, folder_key = parse_folder_link(url)
    if not folder_id:
        raise ValueError(f"not a MEGA folder link: {url}")
    session = session or requests.Session()
    master = _b64_to_a32(folder_key)

    res = _api(folder_id, {"a": "f", "c": 1, "r": 1, "ca": 1}, session)
    files = []
    for node in res.get("f", []):
        if node.get("t") != 0:  # 0 = file
            continue
        try:
            enc = node["k"].split(":")[1]
            key = _decrypt_key(_b64_to_a32(enc), master)
            if len(key) < 8:
                continue
            k = (key[0] ^ key[4], key[1] ^ key[5], key[2] ^ key[6], key[3] ^ key[7])
            iv = (key[4], key[5], 0, 0)
            name = _decrypt_attr(_b64_decode(node["a"]), k).get("n")
            if name:
                files.append({"name": name, "node": node["h"], "key": k,
                              "iv": iv, "size": node.get("s", 0),
                              "folder_id": folder_id})
        except Exception:
            continue
    return files, session


def download_file(info, dest_dir, session=None):
    """Download and decrypt one file entry from list_folder()."""
    session = session or requests.Session()
    res = _api(info["folder_id"], {"a": "g", "g": 1, "n": info["node"]}, session)
    url = res.get("g")
    if not url:
        raise RuntimeError(f"no download URL for {info['name']}")

    ctr = Counter.new(128, initial_value=((info["iv"][0] << 32) + info["iv"][1]) << 64)
    aes = AES.new(_a32_to_str(info["key"]), AES.MODE_CTR, counter=ctr)

    dest = Path(dest_dir) / info["name"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with session.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=128 * 1024):
                if chunk:
                    f.write(aes.decrypt(chunk))
    return dest
