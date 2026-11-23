#!/usr/bin/env python3
"""Create a signed manifest for a distributable AutoZapaz release."""
import argparse, base64, hashlib, json
from getpass import getpass
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
MANIFEST="release-manifest.json"; SIGNATURE="release-manifest.sig"
def b64(data): return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
def main():
 p=argparse.ArgumentParser(description=__doc__); p.add_argument("--release",required=True,type=Path); p.add_argument("--private-key",required=True,type=Path); p.add_argument("--version",required=True); a=p.parse_args()
 root=a.release.resolve()
 if not root.is_dir(): p.error("release directory does not exist")
 if (root/MANIFEST).exists() or (root/SIGNATURE).exists(): p.error("refusing to overwrite existing signature files")
 key=serialization.load_pem_private_key(a.private_key.expanduser().read_bytes(),password=getpass("Release signing-key password: ").encode())
 if not isinstance(key,Ed25519PrivateKey): p.error("release key must be Ed25519")
 files={}
 for path in sorted(root.rglob("*")):
  if path.is_symlink(): p.error("release contains symlink: "+str(path.relative_to(root)))
  if path.is_file():
   rel=path.relative_to(root).as_posix(); data=path.read_bytes(); files[rel]={"size":len(data),"sha256":hashlib.sha256(data).hexdigest()}
 if not files: p.error("release is empty")
 manifest={"format_version":1,"version":a.version,"files":files}; payload=json.dumps(manifest,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
 (root/MANIFEST).write_bytes(payload+b"\n"); (root/SIGNATURE).write_text(b64(key.sign(payload))+"\n",encoding="ascii")
 print("Signed release:",root)
if __name__=="__main__": main()
