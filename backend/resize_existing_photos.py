"""One-off utility to resize existing stored photos down to max dimension 720."""
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / 'data/hiking.sqlite3'


def get_image_info(path):
    cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height',
        '-of', 'csv=s=x:p=0',
        str(path)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10)
    out = res.stdout.strip()
    if 'x' in out:
        w, h = out.split('x')[:2]
        return int(w), int(h)
    return None, None


def resize_photo(file_path, max_dim=720):
    temp_path = file_path.with_name(f"{file_path.stem}_720.tmp.jpg")
    cmd = [
        'ffmpeg', '-y',
        '-i', str(file_path),
        '-vf', f"scale='if(gt(iw,ih),min({max_dim},iw),-2)':'if(gt(iw,ih),-2,min({max_dim},ih))'",
        '-q:v', '3',
        '-update', '1',
        '-frames:v', '1',
        str(temp_path)
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    w, h = get_image_info(temp_path)
    new_size = temp_path.stat().st_size
    os.replace(temp_path, file_path)
    return w, h, new_size


def process_all(db_path=DB_PATH):
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    rows = cursor.execute('SELECT id, filename, file_path, size, width, height FROM photos').fetchall()
    print(f"Total photos found in database: {len(rows)}")

    total_orig_size = 0
    total_new_size = 0
    processed_count = 0

    for r in rows:
        photo_id = r['id']
        file_path = Path(r['file_path'])
        orig_size = r['size'] or 0
        total_orig_size += orig_size

        if not file_path.is_file():
            print(f"[{photo_id}] File not found: {file_path}, skipping")
            total_new_size += orig_size
            continue

        w = r['width'] or 0
        h = r['height'] or 0

        # Resize if max dimension > 720 or file size > 250KB
        if max(w, h) > 720 or orig_size > 250 * 1024:
            try:
                new_w, new_h, new_size = resize_photo(file_path, max_dim=720)
                cursor.execute(
                    'UPDATE photos SET width=?, height=?, size=? WHERE id=?',
                    (new_w, new_h, new_size, photo_id)
                )
                conn.commit()
                processed_count += 1
                total_new_size += new_size
                print(f"[{photo_id}] {r['filename']}: {w}x{h} ({orig_size//1024}KB) -> {new_w}x{new_h} ({new_size//1024}KB)")
            except Exception as e:
                print(f"[{photo_id}] Error resizing {r['filename']}: {e}")
                total_new_size += orig_size
        else:
            total_new_size += orig_size

    conn.close()
    saved = total_orig_size - total_new_size
    saved_mb = saved / (1024 * 1024)
    print(f"\nDone! Processed: {processed_count}/{len(rows)}")
    print(f"Original: {total_orig_size / (1024*1024):.2f} MB -> New: {total_new_size / (1024*1024):.2f} MB (Saved: {saved_mb:.2f} MB)")


if __name__ == '__main__':
    process_all()
