"""The same-origin client script for the Argument catalogue section."""

from matuto.page.catalogue import SCRIPT

PATH = "/argument-catalogue.js"


def handle(request):
    return 200, SCRIPT, "text/javascript"
