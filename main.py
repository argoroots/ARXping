from google.appengine.api import mail, memcache, urlfetch, app_identity
from google.appengine.ext import db
from datetime import date, datetime, timedelta
import jinja2, json, logging, os, time, webapp2


# Database classes
class Site(db.Model):
    url             = db.StringProperty(default='')
    search          = db.StringProperty(default='', indexed=False)
    email           = db.StringProperty(default='', indexed=False)
    is_enabled      = db.BooleanProperty(default=False)

class Ping(db.Model):
    site            = db.ReferenceProperty(Site)
    interval        = db.StringProperty(choices=['day', 'hour'])
    date            = db.DateTimeProperty(auto_now_add=True)
    count           = db.IntegerProperty(default=0, indexed=False)
    time            = db.IntegerProperty(default=0, indexed=False)

class Downtime(db.Model):
    site            = db.ReferenceProperty(Site)
    start_date      = db.DateTimeProperty(auto_now_add=True)
    end_date        = db.DateTimeProperty(indexed=False)
    is_current      = db.BooleanProperty(default=True)
    message         = db.StringProperty()


# Show main page
class Page(webapp2.RequestHandler):
    def get(self):
        jinja_environment = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.dirname(__file__)))
        template = jinja_environment.get_template('template.html')
        self.response.out.write(template.render({ 'sites': db.Query(Site).filter('is_enabled', True).order('url').fetch(10) }))


# Get and save each page request time
class Cron(webapp2.RequestHandler):
    def get(self):
        for site in db.Query(Site).filter('is_enabled', True).fetch(10):
            ping_start = time.time()
            try:
                result = urlfetch.fetch(
                    url=site.url,
                    deadline=1000,
                    headers={'Cache-Control': 'max-age=0'}
                )
                ping_time = int((time.time() - ping_start)*1000)

                if result.status_code == 200 and result.content.decode('utf-8').find(site.search) != -1:
                    down = db.Query(Downtime).filter('site', site).filter('is_current', True).get()
                    if down:
                        down.end_date = datetime.now()
                        down.is_current = False
                        down.put()

                    intervals = {
                        'hour': datetime(datetime.today().year, datetime.today().month, datetime.today().day, datetime.today().hour),
                        'day':  datetime(datetime.today().year, datetime.today().month, datetime.today().day),
                    }
                    for i, d in intervals.iteritems():
                        ping = db.Query(Ping).filter('site', site).filter('interval', i).filter('date', d).get()
                        if not ping:
                            ping = Ping(site=site, interval=i, date=d)
                        ping.time += ping_time
                        ping.count += 1
                        ping.put()

                else:
                    raise Exception('This website is offline.')

            except Exception, es:
                if not db.Query(Downtime, keys_only=True).filter('site', site).filter('is_current', True).get():
                    down = Downtime(site=site)
                    down.message = '%s' % es[:500]
                    down.put()
                SendMail(site.url, site.email, es)
                self.response.out.write(es)


# Return JSON info for response time chart and downtime table
class Ajax(webapp2.RequestHandler):
    def get(self, period, site_id):
        ping_chart = memcache.get('ping_%s_%s' % (period, site_id))
        down_list = memcache.get('downtime_%s_%s' % (period, site_id))

        if period == '24h':
            pings = db.Query(Ping).filter('site', Site().get_by_id(int(site_id))).filter('interval', 'hour').filter('date >', (datetime.now() - timedelta(hours = 25))).order('date')
            downs = db.Query(Downtime).filter('site', Site().get_by_id(int(site_id))).filter('start_date >', (datetime.now() - timedelta(hours = 24))).order('-start_date')
            time_format = '%H'
            tooltip = 'Average response time at %1 was %2ms'
        elif period == '7d':
            pings = db.Query(Ping).filter('site', Site().get_by_id(int(site_id))).filter('interval', 'hour').filter('date >', (datetime.now() - timedelta(days = 7))).order('date')
            downs = db.Query(Downtime).filter('site', Site().get_by_id(int(site_id))).filter('start_date >', (datetime.now() - timedelta(days = 7))).order('-start_date')
            time_format = '%b, %d at %H'
            tooltip = 'Average response time on %1 was %2ms'
        else:
            pings = db.Query(Ping).filter('site', Site().get_by_id(int(site_id))).filter('interval', 'day').order('date')
            downs = db.Query(Downtime).filter('site', Site().get_by_id(int(site_id))).order('-start_date')
            time_format = '%b, %d'
            tooltip = 'Average response time on %1 was %2ms'

        if not ping_chart:
            ping_chart = []
            for ping in pings.fetch(500):
                ping_chart.append({
                    'label': ping.date.strftime(time_format),
                    'value': ping.time/ping.count
                })
            # memcache.add('ping_%s_%s' % (period, site_id), 600)

        if not down_list:
            down_list = []
            for down in downs.fetch(500):
                down_list.append({
                    'begin': down.start_date.strftime('%b, %d at %H:%M'),
                    'end': down.end_date.strftime('%b, %d at %H:%M') if down.end_date else 'NOW',
                    'period': timedeltaFormat((down.end_date - down.start_date).seconds) if down.end_date else timedeltaFormat((datetime.now() - down.start_date).seconds),
                    'message': down.message if down.message else ''
                })
            # memcache.add('downtime_%s_%s' % (period, site_id), 180)

        jsonStr = json.dumps({
            'chart': {
                'tooltip': tooltip,
                'data': ping_chart
            },
            'downtime': down_list
        })

        self.response.out.write(jsonStr);


# Create new site and redirect to GAE edit form
class Create(webapp2.RequestHandler):
    def get(self):
        s = Site()
        s.put()
        self.redirect('https://appengine.google.com/datastore/edit?app_id=s~%s&key=%s' % (app_identity.get_application_id(), s.key()))


# Send email if site is down
def SendMail(url, to, message):
    if memcache.get('EmailSent_%s' % url):
        return

    if type(to) is not list:
        to = [to]

    if len(to) < 1:
        return

    m = mail.EmailMessage()
    m.sender = 'argoroots@gmail.com'
    m.to = to
    m.subject = 'PING: %s' % url
    m.body = '%s\n\n%s' % (message, url)
    m.send()

    memcache.add('EmailSent_%s' % url, True, 300)


# format downtime period
def timedeltaFormat(seconds):
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return ('%02d:%02d' % (hours, minutes))


app = webapp2.WSGIApplication([
    ('/', Page),
    ('/cron', Cron),
    (r'/ajax/(.*)/(.*)', Ajax),
    ('/create', Create),
], debug=True)
