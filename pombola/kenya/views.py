from django.views.generic.base import TemplateView

from .forms import CountyPerformancePetitionForm, CountyPerformanceSenateForm

class CountyPerformanceView(TemplateView):
    """This view displays a page about county performance with calls to action

    There are three major variants of the page that include different
    information."""

    template_name = 'county-performance.html'

    def get_context_data(self, **kwargs):
        context = super(CountyPerformanceView, self).get_context_data(**kwargs)
        context['suppress_banner'] = True
        context['petition_form'] = CountyPerformancePetitionForm()
        context['senate_form'] = CountyPerformanceSenateForm()

        context['show_opportunity'], context['show_threat'] = {
            'o': (True, False),
            't': (False, True),
            'n': (False, False),
            None: (False, False),
        }[self.request.GET.get('variant')]

        return context
