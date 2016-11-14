# HOCRViewer

[![Demo](https://thumbs.gfycat.com/TameThornyAsianpiedstarling-size_restricted.gif)](https://gfycat.com/TameThornyAsianpiedstarling)

Read books in HOCR format with [Mirador](http://projectmirador.org/).

## Requirements
- Python 3.5
- **Optional:** An SQLite version that supports FTS5 (check with
  `sqlite3 ":memory:" "PRAGMA compile_options;" |grep FTS5`)

## Installation
```bash
$ pip install -r requirements.txt
```

## Data format
The **HOCR file** must contain all pages as `ocr_page` elements. These must have
a `title` attribute that contains the following fields (as per the
[HOCR Specification](http://kba.github.io/hocr-spec/1.2/)):

- `ppageno`: The physical page number
- `image`: The relative path (from the HOCR file) to the page image
- `bbox`: The dimensions of the image

Additionally, each `ocr_page` element must have an `id` attribute that
assigns a unique identifier to the page.

Example:
```html
<div class="ocr_page" id="page_0005"
     title="ppageno 4; image spyri_heidi_1880/00000005.tif; bbox 0 0 2013 2985"/>
```

Alternatively, HOCR files with accompanying images that are stored like the
[Google 1000 Books dataset](http://commondatastorage.googleapis.com/books/icdar2007/README.txt)
([download instructions](http://yaroslavvb.blogspot.de/2011/11/google1000-dataset_09.html))
can be indexed and viewed as well.

## Usage
Simply point the application to a directory containing hOCR files and it
will serve a web interface where you can view them:

```bash
$ python hocrviewer.py serve /mnt/data/hocr
```

You can alternatively index your files before serving them. This has two main
advantages: It significantly reduces the response times for the manifests and
annotations and it enables the search within the books (not yet usable from
Mirador, but keep an eye on [this
PR](https://github.com/ProjectMirador/mirador/pull/995)).

To do so, run the `index` subcommand with the path to the directory with
your HOCR files as the first argument. By default, the database will be
written to `~/.config/hocrviewer/hocrviewer.db`, but you can override this
with the `--db-path` option that is passed *before* the subcommand:

```bash
$ python hocrviewer.py --db-path /tmp/test.db index /mnt/data/hocr
```

After the index has been created, run the application with the `serve`
subcommand (making sure that you pass the same `--db-path` value as during
indexing).

```bash
$ python hocrviewer.py --db-path /tmp/test.db serve
```

The application exposes all books as [IIIF](https://iiif.io) manifests at
`/iiif/<book_name>`, where `book_name` is the file name of the HOCR file
for the book without the `.html` extension.

## Planned Features
- Search across all books (backend done, user interface missing)
- Edit OCR with a custom `AnnotationEditor` implementation for Mirador
- Browse books in a paginated view outside of Mirador (which gets overwhelmed
  with large libraries)
