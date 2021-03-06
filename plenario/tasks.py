from datetime import datetime, timedelta

from raven.conf import setup_logging
from raven.handlers.logging import SentryHandler
from sqlalchemy.exc import NoSuchTableError, InternalError

from plenario.celery_app import celery_app
from plenario.database import session as session, app_engine as engine
from plenario.etl.shape import ShapeETL
from plenario.models import MetaTable, ShapeMetadata
from plenario.settings import CELERY_SENTRY_URL
from plenario.etl.point import PlenarioETL
from plenario.utils.weather import WeatherETL

if CELERY_SENTRY_URL:
    handler = SentryHandler(CELERY_SENTRY_URL)
    setup_logging(handler)


@celery_app.task(bind=True)
def delete_dataset(self, source_url_hash):
    md = session.query(MetaTable).get(source_url_hash)
    try:
        dat_table = md.point_table
        dat_table.drop(engine, checkfirst=True)
    except NoSuchTableError:
        # Move on so we can get rid of the metadata
        pass
    session.delete(md)
    try:
        session.commit()
    except InternalError, e:
        raise delete_dataset.retry(exc=e)
    return 'Deleted {0} ({1})'.format(md.human_name, md.source_url_hash)


@celery_app.task(bind=True)
def add_dataset(self, source_url_hash, data_types=None):
    md = session.query(MetaTable).get(source_url_hash)
    session.close()
    if md.result_ids:
        ids = md.result_ids
        ids.append(self.request.id)
    else:
        ids = [self.request.id]
    with engine.begin() as c:
        c.execute(MetaTable.__table__.update()\
            .where(MetaTable.source_url_hash == source_url_hash)\
            .values(result_ids=ids))

    etl = PlenarioETL(md)
    etl.add()
    return 'Finished adding {0} ({1})'.format(md.human_name, md.source_url_hash)


@celery_app.task(bind=True)
def add_shape(self, table_name):
    # Associate the dataset with this celery task
    # so we can check on the task's status
    meta = session.query(ShapeMetadata).get(table_name)
    meta.celery_task_id = self.request.id
    session.commit()

    # Ingest the shapefile
    ShapeETL(meta=meta).add()
    return 'Finished adding shape dataset {} from {}.'.format(meta.dataset_name,
                                                              meta.source_url)


@celery_app.task(bind=True)
def update_shape(self, table_name):
    # Associate the dataset with this celery task
    # so we can check on the task's status
    meta = session.query(ShapeMetadata).get(table_name)
    meta.celery_task_id = self.request.id
    session.commit()

    # Update the shapefile
    ShapeETL(meta=meta).update()
    return 'Finished updating shape dataset {} from {}.'.\
        format(meta.dataset_name, meta.source_url)


@celery_app.task(bind=True)
def delete_shape(self, table_name):
    shape_meta = session.query(ShapeMetadata).get(table_name)
    shape_meta.remove_table()
    session.commit()
    return 'Removed {}'.format(table_name)


@celery_app.task
def frequency_update(frequency):
    # hourly, daily, weekly, monthly, yearly
    md = session.query(MetaTable)\
        .filter(MetaTable.update_freq == frequency)\
        .filter(MetaTable.date_added != None)\
        .all()
    for m in md:
        update_dataset.delay(m.source_url_hash)

    md = session.query(ShapeMetadata)\
        .filter(ShapeMetadata.update_freq == frequency)\
        .filter(ShapeMetadata.is_ingested == True)\
        .all()
    for m in md:
        update_shape.delay(m.dataset_name)
    return '%s update complete' % frequency


@celery_app.task(bind=True)
def update_dataset(self, source_url_hash):
    md = session.query(MetaTable).get(source_url_hash)
    if md.result_ids:
        ids = md.result_ids
        ids.append(self.request.id)
    else:
        ids = [self.request.id]
    with engine.begin() as c:
        c.execute(MetaTable.__table__.update()\
            .where(MetaTable.source_url_hash == source_url_hash)\
            .values(result_ids=ids))
    etl = PlenarioETL(md)
    etl.update()
    return 'Finished updating {0} ({1})'.format(md.human_name, md.source_url_hash)


@celery_app.task
def update_metar():
    print "update_metar()"
    w = WeatherETL()
    w.metar_initialize_current()
    return 'Added current metars'


@celery_app.task()
def hello_world():
    """
    Used in init_db for its side effect.
    Just running a task will create the celery_taskmeta tables in the database/
    """
    print "Hello from celery!"


@celery_app.task
def update_weather():
    # This should do the current month AND the previous month, just in case.

    lastMonth_dt = datetime.now() - timedelta(days=1)
    lastMonth = lastMonth_dt.month
    lastYear = lastMonth_dt.year

    month, year = datetime.now().month, datetime.now().year
    w = WeatherETL()
    if lastMonth != month:
        w.initialize_month(lastYear, lastMonth)
    w.initialize_month(year, month)

    # Given that this was the most recent month, year, call this function,
    # which will figure out the most recent hourly weather observation and
    # delete all metars before that datetime.
    w.clear_metars()
    return 'Added weather for %s %s' % (month, year)
