# HOCRViewer

[![Demo](https://thumbs.gfycat.com/TameThornyAsianpiedstarling-size_restricted.gif)](https://gfycat.com/TameThornyAsianpiedstarling)

Read books in HOCR format with [Mirador](https://mirador-project.org).

## Requirements
- Books in HOCR format with accompanying facsimile images
    - The HOCR file must contain all pages as `ocr_page` elements that
      reference the associated image as a relative path via the `image` value
      in the `title` attribute and include the dimensions of the image via
      the `bbox` value.
    - The images should be in a format that is readable by
      [Pillow](https://pillow.readthedocs.org)
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
