from __future__ import print_function

import logging
import pathlib
from multiprocessing import cpu_count

import click
import click_log
import flask
import gunicorn.app.base
from flask_iiif import IIIF
from flask_iiif.cache.simple import ImageSimpleCache
from flask_restful import Api
from iiif_prezi.factory import ManifestFactory

from index import DocumentRepository


app = flask.Flask('hocrviewer', static_folder='vendor/mirador/build/mirador',
                  static_url_path='/static')
ext = IIIF(app=app)
api = Api(app=app)
ext.init_restful(api, prefix="/iiif/image/")
repository = None
logger = logging.getLogger(__name__)


def locate_image(uid):
    book_id, page_id = uid.split(':')
    return repository.get_image_path(book_id, page_id)


class HocrViewerApplication(gunicorn.app.base.BaseApplication):
    def __init__(self, app, base_url):
        self.options = {'bind': '0.0.0.0:5000',
                        'workers': cpu_count()*2+1}
        self.application = app
        app.config['IIIF_CACHE_HANDLER'] = ImageSimpleCache()
        app.config['BASE_URL'] = base_url
        ext.uuid_to_image_opener_handler(locate_image)
        super(HocrViewerApplication, self).__init__()

    def load_config(self):
        config = dict([(key, value) for key, value in self.options.items()
                       if key in self.cfg.settings and value is not None])
        for key, value in config.items():
            self.cfg.set(key.lower(), value)

    def load(self):
        return self.application


def build_manifest(book_id, book_path, metadata, pages):
    fac = ManifestFactory()
    base_url = app.config.get('BASE_URL', 'http://localhost:5000')
    fac.set_base_metadata_uri(
        base_url + flask.url_for('get_book_manifest', book_id=book_id))
    fac.set_base_image_uri(base_url + '/iiif/image/v2')
    fac.set_iiif_image_info(2.0, 2)
    manifest = fac.manifest(label=book_id)
    manifest.set_description("Automatically generated from HOCR")
    seq = manifest.sequence(ident='0')
    for idx, (page_id, img_path, width, height) in enumerate(pages):
        canvas = seq.canvas(ident=page_id,
                            label='Page {}'.format(idx))
        anno = canvas.annotation(ident=page_id)
        img = anno.image('{}:{}'.format(book_id, page_id), iiif=True)
        img.set_hw(height, width)
        canvas.height = img.height
        canvas.width = img.width
        canvas.annotationList(
            base_url + flask.url_for('get_page_lines', book_id=book_id,
                                     page_id=page_id),
            label="Transcribed Text")
    if not seq.canvases:
        logger.error("{} has no images!".format(book_path))
        return None
    else:
        return manifest


def get_canvas_id(book_id, page_id):
    base_url = app.config.get('BASE_URL', 'http://localhost:5000')
    return (base_url + flask.url_for('get_book_manifest', book_id=book_id) +
            '/canvas/' + page_id + '.json')


@app.route("/iiif/<book_id>")
def get_book_manifest(book_id):
    doc = repository.get_document(book_id)
    pages = repository.get_pages(book_id)
    if not doc:
        flask.abort(404)
    manifest = build_manifest(*doc, pages)
    if manifest is None:
        flask.abort(500)
    return flask.jsonify(manifest.toJSON(top=True))


@app.route("/iiif/<book_id>/list/<page_id>", methods=['GET'])
@app.route("/iiif/<book_id>/list/<page_id>.json", methods=['GET'])
def get_page_lines(book_id, page_id):
    lines = repository.get_lines(book_id, page_id)

    fac = ManifestFactory()
    base_url = app.config.get('BASE_URL', 'http://localhost:5000')
    fac.set_base_metadata_uri(base_url + '/iiif/' + book_id)
    annotation_list = fac.annotationList(ident=page_id)
    for idx, (text, x, y, w, h) in enumerate(lines):
        anno = annotation_list.annotation(ident='line-{}'.format(idx))
        anno.text(text=text)
        anno.on = (get_canvas_id(book_id, page_id) +
                   "#xywh={},{},{},{}".format(x, y, w, h))
    out_data = annotation_list.toJSON(top=True)
    if not annotation_list.resources:
        # NOTE: iiif-prezi strips empty list from the resulting JSON,
        #       so we have to add the empty list ourselves...
        out_data['resources'] = []
    return flask.jsonify(out_data)


@app.route('/')
def index():
    return flask.render_template(
        'mirador.html', book_ids=repository.document_ids())


@click.group()
@click_log.simple_verbosity_option()
@click_log.init()
@click.option('-db', '--db-path', help='Target path for application database',
              type=click.Path(dir_okay=False, readable=True, writable=True),
              default=click.get_app_dir('hocrviewer') + '/hocrviewer.db')
def cli(db_path):
    db_path = pathlib.Path(db_path)
    if not db_path.parent.exists():
        db_path.parent.mkdir(parents=True)
    global repository
    repository = DocumentRepository(db_path)


@cli.command()
@click.option('-u', '--base-url', default='http://127.0.0.1:5000',
              help='HTTP URL where the application is reachable')
def run(base_url):
    HocrViewerApplication(app, base_url).run()


@cli.command('index')
@click.argument('hocr-directory', type=click.Path(file_okay=False, exists=True,
                                                  readable=True))
def index_documents(hocr_directory):
    base_directory = pathlib.Path(hocr_directory)
    hocr_files = tuple(base_directory.glob("*.html"))
    with click.progressbar(
            hocr_files,
            item_show_func=lambda p: p.stem if p else '') as hocr_files:
        for hocr_path in hocr_files:
            try:
                repository.ingest_document(hocr_path)
            except Exception as e:
                logger.error("Could not ingest {}".format(hocr_path))
                logger.exception(e)


if __name__ == '__main__':
    cli()
