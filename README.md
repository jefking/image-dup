## image-dup

Local, dependency-free browser UI to review likely-duplicate photos side-by-side.

### How it groups files

It compares files **only within the same folder**.

Within each folder, it groups by a **normalized filename key**: the filename stem lowercased, with a trailing ` (N)` suffix removed.

Example: `2E1B4361.jpg` and `2E1B4361 (2).jpg` become the same key and will be shown as a pair.

### Run

Move-to-trash mode (default):

```bash
python3 app.py --root /home/jef/Pictures/photos --port 8000
```

Then open: http://127.0.0.1:8000/

### Controls

* Click left/right image to delete it (browser will ask you to confirm).
* `N` or `Right Arrow` skips to the next pair/group.
