import pytz
import dateutil


def process_date(date):
    """
        Parse the input string `date` into a datetime object, convert it to UTC timezone,
        and remove the timezone information.

        :param
        - date (str): A string representing a date and time in a recognizable format.

        :return
        datetime: A datetime object representing the input date and time converted to UTC.
        The returned datetime object has no timezone information.
    """
    return dateutil.parser.parse(date).astimezone(pytz.UTC).replace(tzinfo=None)