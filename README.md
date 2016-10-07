# HOCRViewer

[![Demo](https://thumbs.gfycat.com/TameThornyAsianpiedstarling-size_restricted.gif)](https://gfycat.com/TameThornyAsianpiedstarling)

Read books in HOCR format with [Mirador](https://mirador-project.org).

## Requirements
- Python 3.5
- NodeJS with `npm`, `bower` and `grunt` installed (for Mirador)
- An SQLite version that supports FTS5 (check with
  `sqlite3 ":memory:" "PRAGMA compile_options;" |grep FTS5`)

## Installation
```bash
$ pip install -r requirements.txt
$ git submodule update --init --recursive vendor/mirador
$ cd vendor/mirador
$ npm install
$ bower install
$ grunt
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
Before the web application can be run, the HOCR files that content is to be
served from, have to be indexed. This is for two reasons: To make the
response times for the manifests and annotations bearable and to enable
the search within the books (not yet usable from Mirador, but keep an eye
on [this PR](https://github.com/ProjectMirador/mirador/pull/995)).

To do so, run the `index` subcommand with the path to the directory with
your HOCR  files as the first argument. By default, the database will be
written to `~/.config/hocrviewer/hocrviewer.db`, but you can override this
with the `--db-path` option that is passed *before* the subcommand:

```bash
$  python hocrviewer.py --db-path /tmp/test.db index /mnt/data/hocr
```

After the index has been created, run the application with the `run` subcommand
(making sure that you pass the same `--db-path` value as during indexing).
If you want to expose the application on an URL other than
`http://127.0.0.1:5000` (e.g. because you're using a reverse proxy), specify
the full URL that is to be externally visible with the `--base-url` option
(e.g. `--base-url http://example.com/hocrviewer`).

```bash
$ python hocrviewer.py --db-path /tmp/test.db run --base-url "http://127.0.0.1:5000"
```

The application exposes all books as [IIIF](https://iiif.io) manifests at
`/iiif/<book_name>`, where `book_name` is the file name of the HOCR file
for the book without the `.html` extension.

## Planned Features
- Search across all books (backend done, user interface missing)
- Edit OCR with a custom `AnnotationEditor` implementation for Mirador
- Browse books in a paginated view outside of Mirador (which gets overwhelmed
  with large libraries)
