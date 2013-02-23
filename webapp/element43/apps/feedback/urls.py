from django.conf.urls.defaults import patterns, url

urlpatterns = patterns('apps.feedback.views',
    url(r'^send_feedback/$', 'send_feedback', name='send_feedback'),
)