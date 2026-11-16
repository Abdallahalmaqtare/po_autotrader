"""Safe field-level startup diagnostics. Never stringify exceptions or values."""
from contextlib import contextmanager
from diagnostics import SafeIssue


class SettingsIssue(ValueError):
    def __init__(self, code, field):
        self.code = code
        self.field = field
        super().__init__(code)

    def safe_text(self):
        return f'code={self.code}; field={self.field}'


@contextmanager
def field_check(field, code='SETTINGS_INVALID'):
    try:
        yield
    except SettingsIssue:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, SafeIssue):
        raise SettingsIssue(code, field) from None


def dependency_error(exc, stage):
    if isinstance(exc, SettingsIssue):
        return f'[APP] settings not ready; stage={stage}; {exc.safe_text()}; type={type(exc).__name__}'
    name = type(exc).__name__
    code = {'ServerSelectionTimeoutError':'MONGO_UNREACHABLE',
            'OperationFailure':'MONGO_OPERATION_FAILED',
            'ConfigurationError':'MONGO_CONFIG_INVALID',
            'InvalidURI':'MONGO_URI_INVALID',
            'ValueError':'SETTINGS_VALUE_ERROR'}.get(name, 'DATABASE_DEPENDENCY_FAILED')
    return f'[APP] dependency not ready; stage={stage}; code={code}; type={name}'
