# HOCRViewer

[![Demo](https://thumbs.gfycat.com/TameThornyAsianpiedstarling-size_restricted.gif)](https://gfycat.com/TameThornyAsianpiedstarling)

Read books in HOCR format with [Mirador](https://mirador-project.org).

## Requirements
- Python 2.7
- NodeJS with `npm` and `grunt` installed (for Mirador)

# Installation
- Install all Python dependencies with `pip install -r requirements.txt`
- Fetch and build Mirador:
    ```bash
    $ git submodule update --init --recursive vendor/mirador`
    $ cd vendor/mirador
    $ npm install
    $ bower install
    $ grunt
    ```

# Data format
The **HOCR file** must contain all pages as `ocr_page` elements. These must have
a `title` attribute that contains the following fields (as per the
[HOCR Specification](http://kba.github.io/hocr-spec/1.2/)):
    - `ppageno`: The physical page number
    - `image`: The relative path (from the HOCR file) to the page image
    - `bbox`: The dimensions of the image

Example:
```html
<div class="ocr_page" id="page_0005"
     title="ppageno 4; image spyri_heidi_1880/00000005.tif; bbox 0 0 2013 2985"/>
```

## Usage
Run `hocrviewer.py`, pass the path to the directory with the HOCR files as
the first parameter and the host and port that the application is available
at as the second parameter:

```bash
$ python hocrviewer.py /mnt/data/hocr "http://127.0.0.1:5000"
```

The application exposes all books as [IIIF](https://iiif.io) manifests at
`/iiif/<book_name>`, where `book_name` is the file name of the HOCR file
for the book without the `.html` extension.

## Planned Features
- Search inside the books via [IIIF Search API](http://iiif.io/api/search/0.9/)
- Search across all books
- Edit OCR with a custom `AnnotationEditor` implementation for Mirador
- Browse books in a paginated view outside of Mirador (which gets overwhelmed
  with large libraries)
