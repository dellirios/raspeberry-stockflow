"""Native 203 dpi ZPL; metrics and coordinates ported from the approved agent."""
import math
import re
import unicodedata

UPPER = [.55,.55,.53,.59,.5,.5,.59,.61,.28,.44,.55,.48,.76,.61,.57,.55,.57,.59,.53,.5,.61,.53,.81,.55,.55,.5]
LOWER = [.46,.5,.44,.5,.48,.28,.5,.5,.26,.26,.44,.26,.76,.5,.48,.5,.5,.33,.42,.28,.5,.44,.66,.44,.44,.39]


def js_round(value):
    return math.floor(value + .5)


def content(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', str(value if value is not None else ''))).strip() or 'Não Informado'


def estimate_width(text, font_width):
    total = 0
    for glyph in text:
        char = ''.join(c for c in unicodedata.normalize('NFD', glyph) if not unicodedata.category(c).startswith('M'))
        if len(char) == 1 and 'A' <= char <= 'Z':
            factor = UPPER[ord(char) - 65]
        elif len(char) == 1 and 'a' <= char <= 'z':
            factor = LOWER[ord(char) - 97]
        elif len(char) == 1 and '0' <= char <= '9':
            factor = .48
        elif char in " !'(),./:;[]`":
            factor = .29
        elif char in '#$*':
            factor = .48
        elif char == '&':
            factor = .61
        elif char in '%+-=@':
            factor = .9
        else:
            factor = 1
        total += js_round(font_width * factor)
    return total + max(2, math.ceil(font_width * .15))


def wrap(text, width, font_width):
    lines, line = [], ''
    for word in text.split(' '):
        candidate = (line + ' ' + word) if line else word
        if estimate_width(candidate, font_width) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ''
        for char in word:
            if estimate_width(line + char, font_width) > width:
                lines.append(line)
                line = ''
            line += char
    if line:
        lines.append(line)
    return lines


def measure(name, value, width, height, font=20, min_font=18, max_lines=2, line_gap=1, width_ratio=1):
    text = content(value)
    for size in range(font, min_font - 1, -1):
        font_width = max(7, js_round(size * width_ratio))
        lines = wrap(text, width, font_width)
        used = len(lines) * size + max(0, len(lines) - 1) * line_gap
        if len(lines) <= max_lines and used <= height:
            return dict(name=name, content=text, lines=lines, fontHeight=size, fontWidth=font_width, lineGap=line_gap, usedHeight=used)
    raise ValueError(f'Campo {name} excede a area da etiqueta 60x60 mm; impressao nao enviada')


def escape_data(text):
    return ''.join(chr(b) if 32 <= b <= 126 and b not in (92,94,95,126) else f'_{b:02X}' for b in text.encode('utf-8'))


def qr_geometry(value, box, maximum=3):
    text = unicodedata.normalize('NFC', str(value if value is not None else '')).strip()
    if not text:
        raise ValueError('Codigo de retirada ausente')
    length = len(text.encode('utf-8'))
    version = next((i for i, capacity in enumerate((17,32,53,78,106), 1) if length <= capacity), None)
    if version is None:
        raise ValueError('Conteudo do QR Code excede a area disponivel')
    modules = 17 + 4 * version
    mag = min(maximum, min(box['width'], box['height']) // (modules + 8))
    if mag < 2:
        raise ValueError('QR Code ficaria pequeno demais')
    size = (modules + 8) * mag
    x = box['x'] + (box['width'] - size) // 2
    y = box['y'] + (box['height'] - size) // 2
    quiet = 4 * mag
    return dict(x=x, y=y, width=size, height=size, symbolX=x+quiet, symbolY=y+quiet, symbolSize=modules*mag,
                quiet=quiet, version=version, magnification=mag, bytes=length, data=text)


def validate_elements(elements, left=18, right=462):
    for a in elements:
        if a['x'] < left or a['y'] < 0 or a['x']+a['width'] > right or a['y']+a['height'] > 468:
            raise ValueError('Elemento fora dos limites: ' + a['name'])
    for i, a in enumerate(elements):
        for b in elements[i+1:]:
            if a['x'] < b['x']+b['width'] and a['x']+a['width'] > b['x'] and a['y'] < b['y']+b['height'] and a['y']+a['height'] > b['y']:
                raise ValueError(f"Colisao de layout: {a['name']} / {b['name']}")


class Drawing:
    def __init__(self, zebra=True):
        self.commands = ['^XA','^MTT','^MMT','^PW480','^LL480','^LS0'] + (['^LRN'] if zebra else []) + ['^CI28']
        self.elements, self.cards, self.bars = [], [], []

    def box(self, x, y, width, height, thickness=1, rounding=2):
        self.commands.append(f'^FO{x},{y}^GB{width},{height},{thickness},B,{rounding}^FS')

    def text(self, field, x, y, width, align='L', reverse=False, height=None):
        self.elements.append(dict(field, x=x, y=y, width=width, height=field['usedHeight'] if height is None else height, reverse=reverse))
        for i, line in enumerate(field['lines']):
            measured = estimate_width(line, field['fontWidth'])
            origin = x + ((width-measured)//2 if align == 'C' else width-measured if align == 'R' else 0)
            top = y + i * (field['fontHeight']+field['lineGap'])
            inv = '^FR' if reverse else ''
            self.commands.append(f"^FO{origin},{top}^A0N,{field['fontHeight']},{field['fontWidth']}{inv}^FH_^FD{escape_data(line)}^FS")

    def header(self, name, title, x, y, width, height, capsule=False):
        self.box(x, y, width, height, height, 8 if capsule else 4)
        field = measure(name+':titulo', title, width-12, height-2, font=16, min_font=16, max_lines=1)
        self.bars.append(dict(name=name,x=x,y=y,width=width,height=height))
        self.text(field,x+6,y+(height-field['usedHeight'])//2,width-12,reverse=True)

    def qr(self, qr, include_quiet=False):
        self.elements.append(dict(qr, name='qr', x=qr['x'] if include_quiet else qr['symbolX'], y=qr['y'] if include_quiet else qr['symbolY'],
                                  width=qr['width'] if include_quiet else qr['symbolSize'], height=qr['height'] if include_quiet else qr['symbolSize']))
        self.commands.append(f"^FO{qr['symbolX']},{qr['symbolY']}^BQN,2,{qr['magnification']},L,7^FH_^FDLM,B{qr['bytes']:04d}{escape_data(qr['data'])}^FS")

    def finish(self):
        return '\n'.join(self.commands+['^PQ1,0,1,Y','^XZ'])+'\n'


PROFILES = [(5,4,20,26,54,24,50),(3,2,18,26,42,24,42),(1,1,18,26,32,24,32),
            (1,1,18,24,32,22,32),(1,1,18,22,32,20,32),(1,1,18,20,32,20,32),(1,1,18,18,32,18,32)]


def build_zebra_label(data):
    d = Drawing()
    try:
        name = measure('produto',data.get('nome'),436,36,font=36,min_font=28,max_lines=1)
    except ValueError:
        name = measure('produto',data.get('nome'),436,74,font=28,min_font=20,max_lines=3)
    divider_y = 18+name['usedHeight']+4
    grid_top = divider_y+5
    budget = 324-grid_top
    row_data = [('GRUPO','grupo','CONSERVAÇÃO','conservacao'),('PESO','peso_formatado','SIF / LOTE','sif_lote'),
                ('DATA DE ENTRADA','data_entrada','VALIDADE','validade'),('LOCAL ARMAZENADO','armazenado','RESPONSÁVEL','responsavel_entrada')]
    dates = [f'{title}: {data[key]}' for title,key in [('Val','val_fornecedor'),('Fab','fab_fornecedor')] if data.get(key)]
    supplier_dates = measure('fornecedor:datas','   '.join(dates),275,16,font=16,min_font=16,max_lines=1) if dates else None
    plan = None
    for index, profile in enumerate(PROFILES):
        gap,pad,bar,font,value_height,supplier_font,supplier_height = profile
        try:
            rows = []
            for lt,lk,rt,rk in row_data:
                fields = [(title,measure(title+':valor',data.get(key),206,value_height,font=font,min_font=16,max_lines=3,line_gap=0)) for title,key in [(lt,lk),(rt,rk)]]
                rows.append((fields,bar+pad*2+max(f['usedHeight'] for _,f in fields)))
            supplier = measure('fornecedor:valor',data.get('fornecedor'),432,supplier_height,font=supplier_font,min_font=16,max_lines=3,line_gap=0)
            supplier_h = bar+pad*2+supplier['usedHeight']
            height = sum(h for _,h in rows)+gap*4+supplier_h+18
            if height <= budget:
                plan = (index,rows,supplier,supplier_h,height)
                break
        except ValueError:
            continue
    if plan is None:
        raise ValueError('Nome e campos excedem a area legivel da etiqueta 60x60 mm; impressao nao enviada')
    index,rows,supplier,supplier_h,height = plan
    qr = qr_geometry(data.get('id_produto'),dict(x=313,y=306,width=145,height=145),5)
    if qr['version'] == 1 and qr['magnification'] == 5:
        qr['symbolX'] = js_round(qr['symbolX']+(qr['symbolSize']-126)/2)
        qr['symbolY'] = js_round(qr['symbolY']+(qr['symbolSize']-126)/2)
        qr.update(magnification=6,symbolSize=126,quiet=24,width=174,height=174)
        # shortcut: approved Node geometry has reduced clearance by footer lines; retain until a new physical layout is approved.
        qr['x'],qr['y'] = qr['symbolX']-24,qr['symbolY']-24
    code = measure('codigo:valor',qr['data'],267,52,font=34,min_font=18,max_lines=3)
    code_h = 22+code['usedHeight']
    code_top = 462-code_h
    company = measure('empresa',data.get('nm_empresa'),279,code_top-357,font=26,min_font=16,max_lines=3,line_gap=0)
    cnpj = measure('cnpj',f"CNPJ: {data.get('cnpj') or 'Não Informado'}",279,20,font=20,min_font=20,max_lines=1)
    d.text(name,22,18,436,align='C')
    d.box(18,divider_y,444,1,1,0)
    y = grid_top
    row_extra = min(6,(budget-height)//4)
    for fields,row_h in rows:
        h = row_h+row_extra
        for col,(title,field) in enumerate(fields):
            x = 18+col*226
            d.cards.append(dict(name=title,x=x,y=y,width=218,height=h))
            d.box(x,y,218,h,3)
            d.header(title,title,x,y,218,bar)
            d.text(field,x+6,y+bar+(h-bar-field['usedHeight'])//2,206)
        y += h+gap
    d.cards.append(dict(name='fornecedor',x=18,y=y,width=444,height=supplier_h))
    d.box(18,y,444,supplier_h)
    d.header('fornecedor','FORNECEDOR',18,y,444,bar)
    d.text(supplier,24,y+bar+pad,432)
    if supplier_dates:
        d.text(supplier_dates,20,y+supplier_h+4,275)
    d.box(18,327,279,3,3,0)
    d.text(company,18,333,279)
    d.text(cnpj,18,min(330+company['usedHeight']+5,code_top-24),279)
    d.box(18,code_top-4,279,3,3,0)
    d.box(305,308,3,154,3,0)
    d.cards.append(dict(name='codigo',x=18,y=code_top,width=279,height=code_h))
    d.box(18,code_top+6,279,code_h-6,1,3)
    d.header('codigo','CÓDIGO RETIRADA',18,code_top,min(279,estimate_width('CÓDIGO RETIRADA',16)+18),18,True)
    d.text(code,24,code_top+20,267)
    d.qr(qr)
    validate_elements(d.elements)
    return dict(zpl=d.finish(),elements=d.elements,qr=qr,cards=d.cards,bars=d.bars,
                layout=dict(nameHeight=name['usedHeight'],gridTop=grid_top,footerTop=330,profile=index))


def build_elgin_label(data):
    d = Drawing(False)

    def field(name,value,x,y,width,height,align='L',**options):
        options.setdefault('width_ratio',.85)
        d.text(measure(name,value,width,height,**options),x,y,width,align=align,height=height)

    def rule(name,y):
        d.elements.append(dict(name=name,x=12,y=y,width=424,height=1))
        d.commands.append(f'^FO12,{y}^GB424,1,1^FS')

    def title(name,value,x,y,width,align='L'):
        field(name,value,x,y,width,15,align=align,font=15,min_font=15,max_lines=1,width_ratio=1)

    field('produto',data.get('nome'),12,6,424,40,align='C',font=32,min_font=19)
    rule('cabecalho',47)
    for y,(lt,lk,rt,rk) in zip((48,104,160,216),[
        ('GRUPO','grupo','CONSERVAÇÃO','conservacao'),('PESO','peso_formatado','NIF/LOTE','sif_lote'),
        ('DATA DE ENTRADA','data_entrada','VALIDADE','validade'),('LOCAL ARMAZENADO','armazenado','RESPONSÁVEL','responsavel_entrada')]):
        for x,label,key in [(12,lt,lk),(232,rt,rk)]:
            align = 'R' if x == 232 else 'L'
            title(label+':titulo',label,x,y+2,204,align)
            field(label+':valor',data.get(key),x,y+18,204,36,align=align,font=26,line_gap=0)
        rule('divisoria:'+lt,y+55)
    title('fornecedor:titulo','FORNECEDOR',12,272,424)
    dates = [f'{label}: {data[key]}' for label,key in [('Val','val_fornecedor'),('Fab','fab_fornecedor')] if data.get(key)]
    field('fornecedor:valor',data.get('fornecedor'),12,288,424,18 if dates else 33,font=26,min_font=16,max_lines=1,width_ratio=1)
    if dates:
        title('fornecedor:datas','   '.join(dates),12,307,424)
    rule('fornecedor',323)
    field('empresa',data.get('nm_empresa'),12,327,424,32,align='C',font=19,min_font=16,width_ratio=1,line_gap=0)
    field('cnpj','CNPJ: '+content(data.get('cnpj')),12,360,424,18,align='C',font=18,min_font=18,max_lines=1,width_ratio=1)
    rule('empresa',379)
    qr = qr_geometry(data.get('id_produto'),dict(x=348,y=380,width=88,height=88))
    title('codigo:titulo','CÓDIGO RETIRADA',12,392,320)
    field('codigo:valor',qr['data'],12,410,320,56,font=32,max_lines=3)
    d.qr(qr,True)
    validate_elements(d.elements,12,436)
    return dict(zpl=d.finish(),elements=d.elements,qr=qr)


def apply_settings(zpl, printer):
    geometry = printer.get('geometria')
    if geometry:
        width,height,dpi = [geometry.get(k) for k in ('largura_mm','altura_mm','resolucao_dpi')]
        offset = geometry.get('offset_horizontal_mm')
        if any(type(v) not in (int,float) or not math.isfinite(v) or v <= 0 for v in (width,height,dpi)):
            raise ValueError('Dimensoes/resolucao invalidas')
        if type(offset) not in (int,float) or not math.isfinite(offset) or abs(offset) >= width:
            raise ValueError('Offset horizontal invalido')

        def transform(match):
            body = match[1]
            home = re.search(r'\^LH\s*\d+\s*,\s*(\d+)',body)
            y = home[1] if home else '0'
            fields = re.sub(r'\^PW\s*\d+|\^LL\s*\d+|\^LS\s*-?\d+|\^LH\s*\d+\s*,\s*\d+','',body)
            off = js_round(offset*dpi/25.4)
            return f'^XA\n^PW{js_round(width*dpi/25.4)}\n^LL{js_round(height*dpi/25.4)}\n^LH{max(0,off)},{y}\n^LS{max(0,-off)}\n'+fields.lstrip()+'^XZ'

        zpl = re.sub(r'\^XA([\s\S]*?)\^XZ',transform,zpl)
    if 'intensidade' in printer:
        darkness = printer['intensidade']
        if type(darkness) is not int or not 0 <= darkness <= 30:
            raise ValueError('Intensidade deve estar entre 0 e 30')
        zpl = f'~SD{darkness:02d}\n'+zpl.replace('^XA','^XA\n^MD0')
    return zpl
