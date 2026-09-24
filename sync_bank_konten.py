#!/usr/bin/env python3
"""Sync Bank Konten dari Google Drive Bos ke Hostinger via FTP"""
import json, subprocess, sys, os, tempfile, shutil

FTP_USER = "u1734629"
FTP_PASS = "Sisirkuning123!"
FTP_HOST = "46.17.173.27"
FTP_PATH = "/public_html/report.anaksehatgeneros.com/bank_konten_data.json"
FOLDER_ID = "1e1tBZQWRzHCajWRW2df6TaCamTlKUa3v"

def crawl_drive():
    """Crawl Google Drive folder using gdown"""
    result = subprocess.run(
        ["gdown", "--folder", "--json", FOLDER_ID],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        print(f"Gdown error: {result.stderr}")
        return None
    
    items = json.loads(result.stdout)
    
    files = []
    folder_set = set()
    
    for item in items:
        path = item["path"]
        url = item["url"]
        
        # Extract file ID from URL
        fid = url.split("?id=")[1] if "?id=" in url else url.split("/d/")[1].split("/")[0] if "/d/" in url else ""
        
        # Determine folder from path
        parts = path.split("/")
        if len(parts) >= 2:
            folder = "/".join(parts[:-1])
        else:
            folder = "Root"
        
        folder_set.add(folder)
        filename = parts[-1]
        
        # Get size via gdown
        size_output = subprocess.run(
            ["gdown", "--info", fid] if fid else ["echo", "0"],
            capture_output=True, text=True, timeout=30
        ).stdout
        
        size_mb = 0
        if "size:" in size_output.lower():
            try:
                size_str = size_output.split("size:")[1].split()[0].strip()
                size_mb = round(float(size_str), 1)
            except:
                pass
        
        # Generate thumbnail URL via Google Drive
        thumb_url = f"https://drive.google.com/thumbnail?id={fid}&sz=w400"

        files.append({
            "id": fid,
            "name": filename,
            "folder": folder,
            "drive_url": f"https://drive.google.com/file/d/{fid}/view",
            "thumbnail": thumb_url,
            "size_mb": size_mb
        })
    
    folders = sorted(list(folder_set))
    
    data = {
        "files": files,
        "folders": folders,
        "total": len(files),
        "last_sync": subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%S.000Z"], capture_output=True, text=True).stdout.strip()
    }
    
    return data

def upload_ftp(data):
    """Upload JSON ke Hostinger via FTP"""
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    json.dump(data, tmp, indent=2)
    tmp_path = tmp.name
    tmp.close()
    
    # Cosmetics: write with 2-space indent
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=2)
    
    result = subprocess.run([
        "curl", "-s", "-u", f"{FTP_USER}:{FTP_PASS}",
        "-T", tmp_path,
        f"ftp://{FTP_HOST}{FTP_PATH}"
    ], capture_output=True, text=True, timeout=30)
    
    os.unlink(tmp_path)
    
    if result.returncode != 0:
        print(f"FTP upload error: {result.stderr}")
        return False
    return True

if __name__ == "__main__":
    print("=== Sync Bank Konten ===")
    print("Crawling Drive...")
    data = crawl_drive()
    if not data:
        print("FAILED: crawl error")
        sys.exit(1)
    print(f"Found {data['total']} files in {len(data['folders'])} folders")
    print("Uploading to Hostinger...")
    if upload_ftp(data):
        print(f"DONE: {data['total']} files synced")
    else:
        print("FAILED: FTP upload error")
        sys.exit(1)
