import re
from datetime import datetime, timezone


def format_weight(weight, unit=''):
    value = str(weight if weight is not None else '').strip()
    if re.fullmatch(r'[+-]?\d+(?:[.,]\d+)?', value):
        value = re.sub(r'([.,]\d*?[1-9])0+$', r'\1', value)
        value = re.sub(r'[.,]0+$', '', value)
    return (value + (unit or '')).strip()


def format_date(value, with_time=False):
    text = str(value)
    local = re.fullmatch(r'(\d{2}/\d{2}/\d{4})(?: \d{2}:\d{2})?', text)
    if local:
        return text if with_time else local[1]
    try:
        date = datetime.fromisoformat(text.replace('Z', '+00:00'))
        # Match JavaScript Date: ISO date-only input denotes UTC midnight.
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',text):
            date = date.replace(tzinfo=timezone.utc)
        if date.tzinfo:
            date = date.astimezone()
        return date.strftime('%d/%m/%Y %H:%M' if with_time else '%d/%m/%Y')
    except ValueError:
        return value


def prepare_fields(data):
    fields = dict(data)
    if fields.get('data_entrada'):
        fields['data_entrada'] = format_date(fields['data_entrada'], True)
    for field in ('validade', 'val_fornecedor', 'fab_fornecedor'):
        if fields.get(field):
            fields[field] = format_date(fields[field])
    fields['peso_formatado'] = fields.get('peso_formatado') or format_weight(fields.get('peso'), fields.get('unidade_medida'))
    return fields
