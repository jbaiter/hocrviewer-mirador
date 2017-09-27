from __future__ import print_function

import functools
import logging
import pathlib
from collections import namedtuple
from itertools import chain
from multiprocessing import cpu_count

import click
import click_log
import flask
import gunicorn.app.base
from flask_iiif import IIIF
from flask_iiif.cache.simple import ImageSimpleCache
from flask_restful import Api
from iiif_prezi.factory import ManifestFactory

from index import DatabaseRepository, FilesystemRepository

SearchHit = namedtuple("SearchHit",
                       ("match", "before", "after", "annotations"))


app = flask.Flask('hocrviewer', static_folder='./vendor/mirador',
                  static_url_path='/static')
ext = IIIF(app=app)
api = Api(app=app)
ext.init_restful(api, prefix="/iiif/image/")
repository = None
logger = logging.getLogger(__name__)


class ApiException(Exception):
    status_code = 500

    def __init__(self, message, status_code=None, payload=None):
        Exception.__init__(self)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.payload = payload

    def to_dict(self):
        rv = dict(self.payload or ())
        rv['message'] = self.message
        return rv


@app.errorhandler(ApiException)
def handle_api_exception(error):
    response = flask.jsonify(error.to_dict())
    response.status_code = error.status_code
    return response


def cors(origin='*'):
    """This decorator adds CORS headers to the response"""
    def decorator(f):
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            resp = flask.make_response(f(*args, **kwargs))
            h = resp.headers
            h['Access-Control-Allow-Origin'] = origin
            return resp
        return decorated_function
    return decorator


def locate_image(uid):
    book_id, page_id = uid.split(':')
    return repository.get_image_path(book_id, page_id)


class HocrViewerApplication(gunicorn.app.base.BaseApplication):
    def __init__(self, app):
        self.options = {'bind': '0.0.0.0:5000',
                        'workers': cpu_count()*2+1}
        self.application = app
        app.config['IIIF_CACHE_HANDLER'] = ImageSimpleCache()
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
    base_url = flask.request.url_root[:-1]
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
    base_url = flask.request.url_root[:-1]
    return (base_url + flask.url_for('get_book_manifest', book_id=book_id) +
            '/canvas/' + page_id)


@app.route("/iiif/<book_id>")
@cors('*')
def get_book_manifest(book_id):
    doc = repository.get_document(book_id)
    if not doc:
        raise ApiException(
            "Could not find book with id '{}'".format(book_id), 404)
    pages = repository.get_pages(book_id)
    manifest = build_manifest(*doc, pages=pages)
    if manifest is None:
        raise ApiException(
            "Could not build manifest for book with id '{}'"
            .format(book_id), 404)
    if isinstance(repository, DatabaseRepository):
        manifest.add_service(
            ident=(flask.request.base_url +
                   flask.url_for('search_in_book', book_id=book_id)),
            context='http://iiif.io/api/search/1/context.json',
            profile='http://iiif.io/api/search/1/search')
    return flask.jsonify(manifest.toJSON(top=True))


@app.route("/iiif/<book_id>/list/<page_id>", methods=['GET'])
@app.route("/iiif/<book_id>/list/<page_id>.json", methods=['GET'])
@cors('*')
def get_page_lines(book_id, page_id):
    lines = repository.get_lines(book_id, page_id)
    if lines is None:
        raise ApiException(
            "Could not find lines for page '{}' in book '{}'"
            .format(page_id, book_id), 404)
    fac = ManifestFactory()
    fac.set_base_metadata_uri(
        flask.request.url_root[:-1] + '/iiif/' + book_id)
    annotation_list = fac.annotationList(ident=page_id)
    for idx, (text, x, y, w, h) in enumerate(lines):
        anno = annotation_list.annotation(ident='line-{}'.format(idx))
        anno.text(text=text)
        anno.on = (get_canvas_id(book_id, page_id) +
                   "#xywh={},{},{},{}".format(x, y, w, h))
    out_data = annotation_list.toJSON(top=True)
    if not annotation_list.resources:
        # NOTE: iiif-prezi strips empty lists from the resulting JSON,
        #       so we have to add the empty list ourselves...
        out_data['resources'] = []
    return flask.jsonify(out_data)


@app.route("/iiif/<book_id>/search", methods=['GET'])
@cors('*')
def search_in_book(book_id):
    if not isinstance(repository, DatabaseRepository):
        raise ApiException(
                "Searching is only supported if the content has been indexed. "
                "Please run `hocrviewer index` to do so.", 501)
    base_url = flask.request.url_root[:-1]
    query = flask.request.args.get('q')
    out = {
        '@context': [
            'http://iiif.io/api/presentation/2/context.json',
            'http://iiif.io/api/search/1/context.json'],
        '@id': (base_url + flask.url_for('search_in_book',
                                         book_id=book_id) + '?q=' + query),
        '@type': 'sc:AnnotationList',

        'within': {
            '@type': 'sc:Layer',
            'ignored': [k for k in flask.request.args.keys() if k != 'q']
        },

        'resources': [],
        'hits': []}

    for page_id, match_text, line_infos in repository.search(query, book_id):
        match_text = match_text.split()
        start_idxs = [idx for idx, word in enumerate(match_text)
                      if "<hi>" in word]
        end_idxs = [idx for idx, word in enumerate(match_text)
                    if "</hi>" in word]
        for start_idx, end_idx in zip(start_idxs, end_idxs):
            match = " ".join(match_text[start_idx:end_idx+1])
            match = match.replace("<hi>", "").replace("</hi>", "")
            before = "..." + " ".join(
                match_text[max(0, start_idx-8):start_idx]),
            after = " ".join(match_text[end_idx+1:end_idx+9]) + "..."
            hit = SearchHit(match=match, before=before, after=after,
                            annotations=[])
            match_words = chain.from_iterable(
                ((match_text[w.sequence_pos], w.sequence_pos,
                  w.start_x, l.y_pos, w.end_x - w.start_x, l.height)
                 for w in winfos if start_idx <= w.sequence_pos <= end_idx)
                for l, winfos in line_infos)
            for chars, pos, x, y, w, h in match_words:
                anno = {
                    '@id': "/".join((get_canvas_id(book_id, page_id),
                                     'words', str(pos))),
                    '@type': 'oa:Annotation',
                    'motivation': 'sc:Painting',
                    'resource': {
                        '@type': 'cnt:ContentAsText',
                        'chars': (chars.replace('<hi>', '')
                                       .replace('</hi>', ''))},
                    'on': (get_canvas_id(book_id, page_id) +
                           "#xywh={},{},{},{}".format(x, y, w, h))}
                hit.annotations.append(anno['@id'])
                out['resources'].append(anno)
            out['hits'].append({
                '@type': 'sc:Hit',
                'annotations': hit.annotations,
                'match': hit.match,
                'before': hit.before,
                'after': hit.after})
    return flask.jsonify(out)


@app.route("/iiif/<book_id>/autocomplete", methods=['GET'])
@cors('*')
def autocomplete_in_book(book_id):
    if not isinstance(repository, DatabaseRepository):
        raise ApiException(
                "Autocompletion is only supported if the content has been "
                "indexed. Please run `hocrviewer index` to do so.", 501)
    base_url = flask.request.url_root[:-1]
    query = flask.request.args.get('q')
    min_cnt = int(flask.request.args.get('min', '1'))
    out = {
        "@context": "http://iiif.io/api/search/1/context.json",
        "@id": (base_url +
                flask.url_for('autocomplete_in_book', book_id=book_id) +
                "?q=" + query + ('&min=' + min_cnt if min_cnt > 1 else '')),
        "@type": "search:TermList",
        "ignored": [k for k in flask.request.args.keys()
                    if k not in ('q', 'min')],
        "terms": []}
    for term, cnt in repository.autocomplete(query, book_id, min_cnt):
        out['terms'].append({
            'match': term,
            'count': cnt,
            'url': (base_url +
                    flask.url_for('search_in_book', book_id=book_id) +
                    '?q=' + term)})
    return flask.jsonify(out)


@app.route('/')
def index():
    return flask.render_template(
        'index.html', book_ids=repository.document_ids())


@app.route('/view/<book_id>')
def view(book_id):
    return flask.render_template(
        'mirador.html',
        manifest_uri=flask.url_for('get_book_manifest', book_id=book_id))


@click.group()
@click_log.simple_verbosity_option()
@click_log.init()
@click.pass_context
@click.option('-db', '--db-path', help='Target path for application database',
              type=click.Path(dir_okay=False, readable=True, writable=True),
              default=click.get_app_dir('hocrviewer') + '/hocrviewer.db')
def cli(ctx, db_path):
    db_path = pathlib.Path(db_path)
    ctx.obj['DB_PATH'] = db_path
    if db_path.exists():
        global repository
        repository = DatabaseRepository(db_path)


@cli.command('serve')
@click.argument('base_directory', required=False,
                type=click.Path(file_okay=False, exists=True, readable=True))
def serve(base_directory):
    global repository
    if repository is None:
        if base_directory is None:
            raise click.BadArgumentUsage("Please specify a base directory.")
        repository = FilesystemRepository(pathlib.Path(base_directory))
    HocrViewerApplication(app).run()


@cli.command('index')
@click.argument('hocr-files', nargs=-1,
                type=click.Path(dir_okay=False, exists=True, readable=True))
@click.option('--autocomplete-min-count', type=int, default=5,
              help="Only store terms with at least this frequency for "
                   "autocomplete (going from 5 to 1 doubles the database "
                   "size!)")
@click.pass_context
def index_documents(ctx, hocr_files, autocomplete_min_count):
    def show_fn(hocr_path):
        if hocr_path is None:
            return ''
        else:
            return hocr_path.name
    global repository
    if repository is None:
        repository = DatabaseRepository(ctx.obj['DB_PATH'])

    hocr_files = tuple(pathlib.Path(p) for p in hocr_files)
    with click.progressbar(hocr_files, item_show_func=show_fn) as hocr_files:
        for hocr_path in hocr_files:
            try:
                repository.ingest_document(hocr_path, autocomplete_min_count)
            except Exception as e:
                logger.error("Could not ingest {}".format(hocr_path))
                logger.exception(e)


if __name__ == '__main__':
    cli(obj={})
