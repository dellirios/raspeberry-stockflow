import copy
import json
from pathlib import Path
import unittest

from stockflow.layout import build_zebra_label, build_elgin_label, escape_data, apply_settings
from stockflow.formatting import prepare_fields, format_weight

FIXTURES = Path(__file__).parent / 'fixtures'


class LayoutTests(unittest.TestCase):
    def test_native_zpl_matches_node_golden_files(self):
        cases = json.loads((FIXTURES / 'golden.json').read_text(encoding='utf-8'))
        for sample in cases:
            with self.subTest(sample=sample['id']):
                before = copy.deepcopy(sample['data'])
                render = build_elgin_label if sample['kind'] == 'elgin' else build_zebra_label
                actual = render(sample['data'])
                self.assertEqual(actual['zpl'], sample['zpl'])
                self.assertEqual(actual['qr'], sample['qr'])
                self.assertEqual(sample['data'], before)

    def test_long_names_preserve_all_characters(self):
        cases = json.loads((FIXTURES / 'golden.json').read_text(encoding='utf-8'))
        heights = {}
        for sample in cases:
            if sample['kind'] != 'zebra':
                continue
            label = build_zebra_label(sample['data'])
            for field in label['elements']:
                if 'lines' in field:
                    self.assertEqual(''.join(field['lines']).replace(' ', ''), field['content'].replace(' ', ''))
            heights[sample['id']] = label['layout']['nameHeight']
        self.assertLess(heights['curto'], heights['longo'])

    def test_overflow_and_empty_qr_are_explicit_errors(self):
        for field in ('nome', 'grupo', 'cnpj', 'nm_empresa', 'fornecedor'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_zebra_label({'id_produto': 'TESTE', field: 'A' * 500})
        with self.assertRaises(ValueError):
            build_zebra_label({'nome': 'Arroz', 'id_produto': ''})

    def test_escape_injection_and_accents(self):
        self.assertEqual(escape_data('Ação^~_\\'), 'A_C3_A7_C3_A3o_5E_7E_5F_5C')

    def test_settings_preserve_fields_and_validate_darkness(self):
        zpl = '^XA\n^PW480\n^LL480\n^LS0\n^FO10,20^FDTeste^FS\n^XZ'
        out = apply_settings(zpl, {'intensidade': 15, 'geometria': {
            'largura_mm': 60, 'altura_mm': 60, 'resolucao_dpi': 203, 'offset_horizontal_mm': -1}})
        self.assertTrue(out.startswith('~SD15\n'))
        self.assertIn('^LS8', out)
        self.assertIn('^FO10,20^FDTeste^FS', out)
        for value in (31, -1, True, '15'):
            with self.assertRaises(ValueError):
                apply_settings(zpl, {'intensidade': value})

    def test_formatting_preserves_business_values(self):
        self.assertEqual(format_weight('0.500000000000', 'kg'), '0.5kg')
        self.assertEqual(format_weight('0,000', 'kg'), '0kg')
        raw = {'data_entrada': '09/11/2026 15:30', 'validade': '2026-11-12T12:00:00', 'peso': '1.000', 'unidade_medida': 'kg'}
        out = prepare_fields(raw)
        self.assertEqual(out['data_entrada'], raw['data_entrada'])
        self.assertEqual(out['validade'], '12/11/2026')
        self.assertEqual(out['peso_formatado'], '1kg')
