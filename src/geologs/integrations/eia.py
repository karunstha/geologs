"""Weekly US retail fuel prices from the Energy Information Administration (EIA)."""
import json
import urllib.parse
import urllib.request

from .. import config

EIA_URL = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"


def api_key():
    return config.eia_key()


def weekly_prices(area, product, weeks):
    """The latest `weeks` weekly prices for an area and product, newest first.

    Each row has the week's `period` (YYYY-MM-DD) and its `value` in US dollars
    per gallon; a week without a price has an empty value.
    """
    params = [("api_key", api_key()), ("frequency", "weekly"), ("data[0]", "value"),
              ("facets[duoarea][]", area), ("facets[product][]", product),
              ("sort[0][column]", "period"), ("sort[0][direction]", "desc"), ("length", str(weeks))]
    with urllib.request.urlopen(EIA_URL + "?" + urllib.parse.urlencode(params), timeout=20) as response:
        return json.loads(response.read().decode())["response"]["data"]
