#!/usr/bin/env python3
"""Verify an AutoZapaz release manifest signature and every file digest."""
import argparse, base64, hashlib, json, os, re, sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
MANIFEST="release-manifest.json"; SIGNATURE="release-manifest.sig"
def decode_b64(v): return base64.urlsafe_b64decode(v+"="*(-len(v)%4))
def load_key(v):
 v=v.strip(); key=serialization.load_pem_public_key(v.encode()) if v.startswith("-----BEGIN") else Ed25519PublicKey.from_public_bytes(decode_b64(v))
 if not isinstance(key,Ed25519PublicKey): raise ValueError("release public key must be Ed25519")
 return key
def reject_duplicate_keys(pairs):\n result={}\n for key,value in pairs:\n  if key in result: raise ValueError("duplicate manifest key: "+key)\n  result[key]=value\n return result\ndef verify_release(root, public_key, expected_version=None):
 root=Path(root).resolve(); mp=root/MANIFEST; sp=root/SIGNATURE
 if not mp.is_file() or not sp.is_file(): raise ValueError("signed release manifest or signature missing")
 payload=mp.read_bytes().strip(); sig=decode_b64(sp.read_text(encoding="ascii").strip()); load_key(public_key).verify(sig,payload)
 m=json.loads(payload,object_pairs_hook=reject_duplicate_keys)
 if type(m.get("format_version")) is not int or m["format_version"]!=1: raise ValueError("unsupported manifest format")
 version=m.get("version")
 if not isinstance(version,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",version): raise ValueError("invalid release version")
 if expected_version and version!=expected_version: raise ValueError("release version mismatch")
 files=m.get("files")
 if not isinstance(files,dict) or not files: raise ValueError("manifest file list invalid")
 actual=set()
 for p in root.rglob("*"):
  if p.is_symlink(): raise ValueError("release contains symlink")
  if p.is_file() and p.relative_to(root).as_posix() not in (MANIFEST,SIGNATURE): actual.add(p.relative_to(root).as_posix())
 if actual!=set(files): raise ValueError("release file list mismatch")
 for rel,meta in files.items():
  if not isinstance(rel,str) or not rel or rel.startswith("/") or ".." in Path(rel).parts: raise ValueError("unsafe manifest path")
  if not isinstance(meta,dict) or type(meta.get("size")) is not int or meta["size"]<0: raise ValueError("invalid file metadata")
  digest=meta.get("sha256")
  if not isinstance(digest,str) or not re.fullmatch(r"[0-9a-f]{64}",digest): raise ValueError("invalid SHA256")
  f=root/rel
  if not f.is_file() or f.stat().st_size!=meta["size"]: raise ValueError("size mismatch: "+rel)
  if hashlib.sha256(f.read_bytes()).hexdigest()!=digest: raise ValueError("digest mismatch: "+rel)
 return version
def main():
 p=argparse.ArgumentParser(description=__doc__); p.add_argument("--release",required=True,type=Path); p.add_argument("--public-key",default=os.environ.get("AUTOZAPAZ_RELEASE_PUBLIC_KEY","")); p.add_argument("--version"); a=p.parse_args()
 if not a.public_key: p.error("release public key required")
 try: v=verify_release(a.release,a.public_key,a.version)
 except Exception as e: print("Release verification failed:",e,file=sys.stderr); raise SystemExit(1)
 print("Verified signed release:",v)
if __name__=="__main__": main()
