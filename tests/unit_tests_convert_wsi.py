#!/usr/bin/env python3

"""Tests for the offline legacy-WSI to resource/clinical file converter.

The emitted files are run through the real validateData validators, and the
committed Java integration-test fixture is checked to be current converter
output.
"""

import json
import logging.handlers
import shutil
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from importer import cbioportal_common
from importer import convertWsiToResources as converter
from importer import validateData


FIXTURE_DIR = Path('test_data/wsi_convert')
STUDY_FIXTURE_DIR = FIXTURE_DIR / 'study'
JAVA_FIXTURE_DIR = Path('../src/test/resources/wsi_resources')
BASE_URL = 'https://portal.example.org/cbioportal'
STUDY_ID = 'wsi_convert_test'
SAMPLE_TO_PATIENT = {
    'WSI-P1-S1': 'WSI-P1',
    'WSI-P1-S2': 'WSI-P1',
    'WSI-P2-S1': 'WSI-P2',
    'WSI+P3-S1': 'WSI+P3',
}
MERGED_FILES = [
    'data_clinical_patients.txt',
    'data_clinical_samples.txt',
    'data_resource_definition.txt',
    'data_resource_patient.txt',
    'data_resource_sample.txt',
    'meta_clinical_patients.txt',
    'meta_clinical_samples.txt',
    'meta_resource_definition.txt',
    'meta_resource_patient.txt',
    'meta_resource_sample.txt',
]
EXPECTED_FILES = [
    'data_clinical_patient_wsi_counts.txt',
    'data_clinical_sample_wsi_counts.txt',
    'data_resource_definition.txt',
    'data_resource_patient.txt',
    'data_resource_sample.txt',
    'meta_clinical_patient_wsi_counts.txt',
    'meta_clinical_sample_wsi_counts.txt',
    'meta_resource_definition.txt',
    'meta_resource_patient.txt',
    'meta_resource_sample.txt',
]
NO_ALLOWLIST = {'WSI_ALLOWED_SOURCE_PREFIXES': '', 'WSI_ALLOWED_THUMBNAIL_PREFIXES': ''}


def data_rows(path):
    """Return the non-comment rows of a TSV file as lists of raw cells."""
    return [line.split('\t') for line in Path(path).read_text(encoding='utf-8').splitlines()
            if not line.startswith('#')]


def rows_by_image(path):
    header, *rows = data_rows(path)
    records = [dict(zip(header, row)) for row in rows]
    return {json.loads(record['METADATA'])['image_id']: record for record in records}


class ConverterTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.out = Path(self.tmp.name) / 'out'

    def tearDown(self):
        self.tmp.cleanup()

    def convert(self, meta=FIXTURE_DIR / 'meta_wsi.txt', base_url=BASE_URL + '/', study_dir=None):
        return converter.convert(meta, self.out, base_url, study_dir)

    def write_legacy(self, rows, header=None):
        """Write a legacy pair made of the fixture's comment rows and the given data rows."""
        source = (FIXTURE_DIR / 'data_wsi.txt').read_text(encoding='utf-8').splitlines()
        legacy = Path(self.tmp.name) / 'legacy'
        legacy.mkdir(exist_ok=True)
        shutil.copy(FIXTURE_DIR / 'meta_wsi.txt', legacy / 'meta_wsi.txt')
        lines = source[:4] + [header if header is not None else source[4]] + rows
        (legacy / 'data_wsi.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return legacy / 'meta_wsi.txt'

    def copy_study(self):
        """Copy the study-like fixture (clinical sample and patient files) to a scratch dir."""
        study = Path(self.tmp.name) / 'study'
        shutil.copytree(STUDY_FIXTURE_DIR, study)
        return study

    def fixture_rows(self):
        return (FIXTURE_DIR / 'data_wsi.txt').read_text(encoding='utf-8').splitlines()[5:]


class ConvertedOutputTestCase(ConverterTestCase):

    def test_writes_expected_file_pairs(self):
        written = self.convert()
        self.assertEqual(EXPECTED_FILES, sorted(path.name for path in written))
        self.assertEqual(EXPECTED_FILES, sorted(path.name for path in self.out.iterdir()))
        meta = (self.out / 'meta_resource_sample.txt').read_text()
        self.assertEqual(
            'cancer_study_identifier: wsi_convert_test\nresource_type: SAMPLE\n'
            'data_filename: data_resource_sample.txt\n', meta)
        meta = (self.out / 'meta_clinical_sample_wsi_counts.txt').read_text()
        self.assertEqual(
            'cancer_study_identifier: wsi_convert_test\ngenetic_alteration_type: CLINICAL\n'
            'datatype: SAMPLE_ATTRIBUTES\ndata_filename: data_clinical_sample_wsi_counts.txt\n', meta)

    def test_sample_and_patient_rows(self):
        self.convert()
        sample_header = data_rows(self.out / 'data_resource_sample.txt')[0]
        self.assertEqual(['PATIENT_ID', 'SAMPLE_ID', 'RESOURCE_ID', 'URL', 'DISPLAY_NAME', 'TYPE',
                          'METADATA'], sample_header)
        samples = rows_by_image(self.out / 'data_resource_sample.txt')
        patients = rows_by_image(self.out / 'data_resource_patient.txt')
        self.assertEqual(['IMG-1', 'IMG-2', 'IMG-3', 'IMG 7/A&B'], list(samples))
        self.assertEqual(['IMG-4', 'IMG-6'], list(patients))
        self.assertEqual({(r['PATIENT_ID'], r['SAMPLE_ID'], r['RESOURCE_ID'], r['TYPE'])
                          for r in samples.values()},
                         {('WSI-P1', 'WSI-P1-S1', 'WSI_SAMPLE', 'WHOLE_SLIDE_IMAGE'),
                          ('WSI-P1', 'WSI-P1-S2', 'WSI_SAMPLE', 'WHOLE_SLIDE_IMAGE'),
                          ('WSI-P2', 'WSI-P2-S1', 'WSI_SAMPLE', 'WHOLE_SLIDE_IMAGE')})
        self.assertEqual({(r['PATIENT_ID'], r['RESOURCE_ID']) for r in patients.values()},
                         {('WSI-P1', 'WSI_PATIENT'), ('WSI+P3', 'WSI_PATIENT')})
        self.assertNotIn('SAMPLE_ID', data_rows(self.out / 'data_resource_patient.txt')[0])

    def test_viewer_urls_are_absolute_and_encoded(self):
        self.convert()
        samples = rows_by_image(self.out / 'data_resource_sample.txt')
        patients = rows_by_image(self.out / 'data_resource_patient.txt')
        self.assertEqual(
            'https://portal.example.org/cbioportal/wsi/patient/WSI-P2'
            '?studyId=wsi_convert_test&imageId=IMG%207%2FA%26B',
            samples['IMG 7/A&B']['URL'])
        self.assertEqual(
            'https://portal.example.org/cbioportal/wsi/patient/WSI%2BP3'
            '?studyId=wsi_convert_test&imageId=IMG-6',
            patients['IMG-6']['URL'])

    def test_raw_tsv_metadata_round_trips(self):
        self.convert()
        text = (self.out / 'data_resource_sample.txt').read_text(encoding='utf-8')
        self.assertNotIn('""', text, 'metadata must not be CSV-quoted')
        for line in text.splitlines()[1:]:
            self.assertTrue(line.split('\t')[-1].startswith('{'))
        samples = rows_by_image(self.out / 'data_resource_sample.txt')
        first = json.loads(samples['IMG-1']['METADATA'])
        self.assertIs(True, first['is_hne'])
        self.assertIs(False, first['is_ihc'])
        self.assertIs(True, first['can_serve_tiles'])
        self.assertEqual(0, first['timeline_start_days'])
        self.assertEqual(716956681, first['file_size_bytes'])
        self.assertEqual('1', first['part_number'])
        self.assertEqual('Left "upper" lobe \\ wedge', first['part_description'])
        self.assertEqual("Adenocarcinoma, 'acinar'", first['path_dx_title'])
        self.assertEqual('WSI-P1-S1', first['reference_sample_id'])
        self.assertEqual('Recorded procedure date relative to first tumor sequencing',
                         first['timepoint_source'])
        serving = first['wsi_serving']
        self.assertEqual(256, serving['thumbnail_width'])
        self.assertEqual(192, serving['thumbnail_height'])
        self.assertEqual({'height': 768, 'width': 1024}, serving['tile_metadata_json']['dimensions'])
        self.assertEqual({'model': 'Scan "Q" \\ 40', 'objective_power': 40, 'calibrated': True},
                         serving['tile_metadata_json']['vendor']['scanner'])
        second = json.loads(samples['IMG-2']['METADATA'])
        self.assertEqual(-17, second['timeline_start_days'])
        # UNMATCHED reference samples are dropped, as the native importer stored null
        self.assertNotIn('reference_sample_id', json.loads(samples['IMG 7/A&B']['METADATA']))
        self.assertEqual(-365, json.loads(samples['IMG 7/A&B']['METADATA'])['timeline_start_days'])
        self.assertEqual(0, json.loads(samples['IMG 7/A&B']['METADATA'])['file_size_bytes'])

    def test_unservable_slides_have_no_serving_fields(self):
        self.convert()
        samples = rows_by_image(self.out / 'data_resource_sample.txt')
        patients = rows_by_image(self.out / 'data_resource_patient.txt')
        # IMG-2 has a SOURCE_URL and a thumbnail width in the legacy row but CAN_SERVE_TILES=FALSE
        second = json.loads(samples['IMG-2']['METADATA'])
        self.assertIs(False, second['can_serve_tiles'])
        self.assertEqual({}, second['wsi_serving'])
        self.assertEqual({}, json.loads(patients['IMG-6']['METADATA'])['wsi_serving'])

    def test_count_rows_match_native_semantics(self):
        self.convert()
        self.assertEqual(
            [['PATIENT_ID', 'SAMPLE_ID', 'WSI_SAMPLE_SLIDE_COUNT', 'WSI_SAMPLE_PART_MATCHED_SLIDE_COUNT',
              'WSI_SAMPLE_BLOCK_MATCHED_SLIDE_COUNT'],
             ['WSI-P1', 'WSI-P1-S1', '2', '1', '1'],
             ['WSI-P1', 'WSI-P1-S2', '1', '0', '1'],
             ['WSI-P2', 'WSI-P2-S1', '1', '1', '0']],
            data_rows(self.out / 'data_clinical_sample_wsi_counts.txt'))
        self.assertEqual(
            [['PATIENT_ID', 'WSI_PATIENT_SLIDE_COUNT', 'WSI_PATIENT_PART_MATCHED_SLIDE_COUNT',
              'WSI_PATIENT_BLOCK_MATCHED_SLIDE_COUNT'],
             ['WSI-P1', '4', '1', '2'],
             ['WSI-P2', '1', '1', '0'],
             ['WSI+P3', '1', '0', '0']],
            data_rows(self.out / 'data_clinical_patient_wsi_counts.txt'))
        header = (self.out / 'data_clinical_sample_wsi_counts.txt').read_text().splitlines()[:4]
        self.assertEqual(
            ['#Patient Identifier\tSample Identifier\tWSI Slides per Sample\t'
             'WSI Slides per Sample, Part-matched\tWSI Slides per Sample, Block-matched',
             '#Patient identifier\tSample identifier\tAssociated pathology slide count for the sample.\t'
             'Associated pathology slides matched to a specimen part.\t'
             'Associated pathology slides matched to a specimen block.',
             '#STRING\tSTRING\tNUMBER\tNUMBER\tNUMBER',
             '#1\t1\t1\t1\t1'], header)
        header = (self.out / 'data_clinical_patient_wsi_counts.txt').read_text().splitlines()[:4]
        self.assertEqual(
            ['#Patient Identifier\tWSI Slides per Patient\tWSI Slides per Patient, Part-matched\t'
             'WSI Slides per Patient, Block-matched',
             '#Patient identifier\tAssociated pathology slide count for the patient.\t'
             'Associated pathology slides matched to a specimen part for the patient.\t'
             'Associated pathology slides matched to a specimen block for the patient.',
             '#STRING\tNUMBER\tNUMBER\tNUMBER',
             '#1\t1\t1\t1'], header)

    def test_only_pairs_with_rows_are_written(self):
        match_level = converter.COLUMNS.index('MATCH_LEVEL')
        unmatched = [row for row in self.fixture_rows() if row.split('\t')[match_level] == 'UNMATCHED']
        written = self.convert(meta=self.write_legacy(unmatched))
        self.assertEqual(
            ['data_clinical_patient_wsi_counts.txt', 'data_resource_definition.txt',
             'data_resource_patient.txt', 'meta_clinical_patient_wsi_counts.txt',
             'meta_resource_definition.txt', 'meta_resource_patient.txt'],
            sorted(path.name for path in written))
        definitions = data_rows(self.out / 'data_resource_definition.txt')
        self.assertEqual(['WSI_PATIENT'], [row[0] for row in definitions[1:]])

    def test_java_fixture_is_current_converter_output(self):
        self.convert(base_url=BASE_URL)
        for name in EXPECTED_FILES:
            self.assertEqual((self.out / name).read_text(encoding='utf-8'),
                             (JAVA_FIXTURE_DIR / name).read_text(encoding='utf-8'),
                             '%s is stale; regenerate it with convertWsiToResources.py' % name)


class ConverterInputTestCase(ConverterTestCase):

    def assertConversionError(self, text, **kwargs):
        with self.assertRaises(converter.ConversionError) as context:
            self.convert(**kwargs)
        self.assertIn(text, str(context.exception))

    def test_portal_base_url_must_be_absolute_http(self):
        for value in ('/cbioportal', 'portal.example.org', 'ftp://portal.example.org',
                      'https://portal.example.org/?x=1'):
            self.assertConversionError('--portal-base-url', base_url=value)

    def test_header_must_match_format_v3(self):
        header = (FIXTURE_DIR / 'data_wsi.txt').read_text().splitlines()[4]
        swapped = header.replace('PATIENT_ID\tREFERENCE_SAMPLE_ID', 'REFERENCE_SAMPLE_ID\tPATIENT_ID')
        self.assertConversionError('invalid header', meta=self.write_legacy(self.fixture_rows(), swapped))

    def test_match_level_must_agree_with_sample(self):
        rows = self.fixture_rows()
        rows[0] = rows[0].replace('\tBLOCK\t', '\tUNMATCHED\t', 1)
        self.assertConversionError('UNMATCHED rows cannot have SAMPLE_ID', meta=self.write_legacy(rows))
        rows = self.fixture_rows()
        rows[3] = rows[3].replace('\tUNMATCHED\t', '\tPART\t', 1)
        self.assertConversionError('matched rows require SAMPLE_ID', meta=self.write_legacy(rows))

    def test_slide_type_and_stain_flags_follow_native_constraints(self):
        slide_type = converter.COLUMNS.index('SLIDE_TYPE')
        is_hne = converter.COLUMNS.index('IS_HNE')
        for column, value, message in ((slide_type, 'Frozen', 'SLIDE_TYPE must be one of'),
                                       (slide_type, '', 'SLIDE_TYPE must be one of'),
                                       (is_hne, 'FALSE', 'inconsistent with SLIDE_TYPE H&E')):
            rows = self.fixture_rows()
            fields = rows[0].split('\t')
            fields[column] = value
            rows[0] = '\t'.join(fields)
            self.assertConversionError(message, meta=self.write_legacy(rows))

    def test_duplicate_image_is_rejected(self):
        rows = self.fixture_rows()
        self.assertConversionError('IMAGE_ID is not unique', meta=self.write_legacy(rows + rows[:1]))

    def test_inconsistent_timing_is_rejected(self):
        rows = self.fixture_rows()
        rows[1] = rows[1].replace('\t-17\tAVAILABLE\t', '\t\tAVAILABLE\t', 1)
        self.assertConversionError('AVAILABLE timing is inconsistent', meta=self.write_legacy(rows))

    def test_line_break_in_output_cell_is_rejected(self):
        rows = self.fixture_rows()
        rows[0] = rows[0].replace('\tIMG-1\t', '\tIMG\r1\t', 1)
        self.assertConversionError('tab or line break', meta=self.write_legacy(rows))
        with self.assertRaises(converter.ConversionError):
            converter.write_tsv(Path(self.tmp.name) / 'x.txt', [['a\tb']])

    def test_existing_count_attributes_conflict(self):
        study = Path(self.tmp.name) / 'study'
        study.mkdir()
        (study / 'data_clinical_samples.txt').write_text(textwrap.dedent('''\
            #Patient Identifier\tSample Identifier\tSlides
            #Patient\tSample\tSlides
            #STRING\tSTRING\tNUMBER
            #1\t1\t1
            PATIENT_ID\tSAMPLE_ID\tWSI_SAMPLE_SLIDE_COUNT
            WSI-P1\tWSI-P1-S1\t2
            '''))
        self.assertConversionError('data_clinical_samples.txt already defines WSI_SAMPLE_SLIDE_COUNT',
                                   study_dir=study)
        self.assertFalse(self.out.exists(), 'nothing may be written after a conflict')

    def test_existing_resource_files_conflict(self):
        study = Path(self.tmp.name) / 'study'
        study.mkdir()
        (study / 'meta_resource_definition.txt').write_text(
            'cancer_study_identifier: wsi_convert_test\nresource_type: DEFINITION\n'
            'data_filename: data_resource_definition.txt\n')
        self.assertConversionError('meta_resource_definition.txt already provides a DEFINITION',
                                   study_dir=study)

    def test_output_dir_must_differ_from_study_dir(self):
        study = self.copy_study()
        self.out = study
        self.assertConversionError('--output-dir must differ from --study-dir', study_dir=study)


class ClinicalMergeTestCase(ConverterTestCase):

    """--study-dir merges the counts into copies of the study's clinical files."""

    def assertConversionError(self, text, **kwargs):
        with self.assertRaises(converter.ConversionError) as context:
            self.convert(**kwargs)
        self.assertIn(text, str(context.exception))
        self.assertFalse(self.out.exists(), 'nothing may be written after a failure')

    def test_writes_merged_clinical_files_under_study_names(self):
        study = self.copy_study()
        written = self.convert(study_dir=study)
        self.assertEqual(MERGED_FILES, sorted(path.name for path in written))
        for meta in ('meta_clinical_samples.txt', 'meta_clinical_patients.txt'):
            self.assertEqual((study / meta).read_bytes(), (self.out / meta).read_bytes())
        # the resource files are the same as without --study-dir
        resources = Path(self.tmp.name) / 'resources'
        converter.convert(FIXTURE_DIR / 'meta_wsi.txt', resources, BASE_URL)
        for name in MERGED_FILES:
            if 'resource' in name:
                self.assertEqual((resources / name).read_bytes(), (self.out / name).read_bytes())

    def test_merged_values_and_na(self):
        study = self.copy_study()
        self.convert(study_dir=study)
        self.assertEqual(
            [['PATIENT_ID', 'SAMPLE_ID', 'CANCER_TYPE', 'TUMOR_PURITY', 'WSI_SAMPLE_SLIDE_COUNT',
              'WSI_SAMPLE_PART_MATCHED_SLIDE_COUNT', 'WSI_SAMPLE_BLOCK_MATCHED_SLIDE_COUNT'],
             ['WSI-P1', 'WSI-P1-S1', 'Breast Cancer', '0.45', '2', '1', '1'],
             ['WSI-P1', 'WSI-P1-S2', 'Breast Cancer', 'NA', '1', '0', '1'],
             ['WSI-P2', 'WSI-P2-S1', 'Breast Cancer', '0.8', '1', '1', '0'],
             ['WSI+P3', 'WSI+P3-S1', 'Breast Cancer', '', 'NA', 'NA', 'NA'],
             ['WSI-P4', 'WSI-P4-S1', 'Breast Cancer', '0.1', 'NA', 'NA', 'NA']],
            data_rows(self.out / 'data_clinical_samples.txt'))
        patients = data_rows(self.out / 'data_clinical_patients.txt')
        self.assertEqual(['WSI_PATIENT_SLIDE_COUNT', 'WSI_PATIENT_PART_MATCHED_SLIDE_COUNT',
                          'WSI_PATIENT_BLOCK_MATCHED_SLIDE_COUNT'], patients[0][-3:])
        self.assertEqual({'WSI-P1': ['4', '1', '2'], 'WSI-P2': ['1', '1', '0'],
                          'WSI+P3': ['1', '0', '0'], 'WSI-P4': ['NA', 'NA', 'NA']},
                         {row[0]: row[-3:] for row in patients[1:]})
        header = (self.out / 'data_clinical_samples.txt').read_text().splitlines()[:4]
        self.assertEqual(
            ['#Patient Identifier\tSample Identifier\tCancer Type\tTumor Purity\t'
             'WSI Slides per Sample\tWSI Slides per Sample, Part-matched\t'
             'WSI Slides per Sample, Block-matched',
             '#Patient identifier\tSample identifier\tCancer type\tEstimated tumor purity\t'
             'Associated pathology slide count for the sample.\t'
             'Associated pathology slides matched to a specimen part.\t'
             'Associated pathology slides matched to a specimen block.',
             '#STRING\tSTRING\tSTRING\tNUMBER\tNUMBER\tNUMBER\tNUMBER',
             '#1\t1\t1\t1\t1\t1\t1'], header)

    def test_existing_lines_are_preserved_byte_for_byte(self):
        study = self.copy_study()
        path = study / 'data_clinical_samples.txt'
        # CRLF on the first rows, a trailing comment, a blank line and no final newline
        original = (path.read_bytes().replace(b'\n', b'\r\n', 3)
                    + b'# trailing comment\n\nWSI-P5\tWSI-P5-S1\tBreast Cancer\t0.2')
        patients = study / 'data_clinical_patients.txt'
        patients.write_text(patients.read_text() + 'WSI-P5\t0:LIVING\t1\t0:DiseaseFree\t1\tMale\n')
        path.write_bytes(original)
        self.convert(study_dir=study)
        merged = (self.out / 'data_clinical_samples.txt').read_bytes()
        original_lines = original.split(b'\n')
        merged_lines = merged.split(b'\n')
        self.assertEqual(len(original_lines), len(merged_lines))
        for before, after in zip(original_lines, merged_lines):
            ending = b'\r' if before.endswith(b'\r') else b''
            if before.startswith(b'# trailing') or not before.strip():
                # comment and blank lines stay unchanged
                self.assertEqual(before, after)
            else:
                self.assertTrue(after.startswith(before.rstrip(b'\r') + b'\t'), (before, after))
                self.assertTrue(after.endswith(ending))

    def test_count_columns_already_present(self):
        study = self.copy_study()
        path = study / 'data_clinical_patients.txt'
        path.write_text(path.read_text().replace('\tSEX\n', '\tWSI_PATIENT_SLIDE_COUNT\n'))
        self.assertConversionError(
            'data_clinical_patients.txt already defines WSI_PATIENT_SLIDE_COUNT', study_dir=study)

    def test_wsi_sample_missing_from_clinical_file(self):
        study = self.copy_study()
        path = study / 'data_clinical_samples.txt'
        path.write_text(path.read_text().replace('WSI-P2\tWSI-P2-S1\tBreast Cancer\t0.8\n', ''))
        self.assertConversionError(
            'data_clinical_samples.txt: SAMPLE_ID with WSI slides not found in the clinical file: '
            'WSI-P2-S1', study_dir=study)

    def test_wsi_patient_missing_from_clinical_file(self):
        study = self.copy_study()
        path = study / 'data_clinical_patients.txt'
        lines = [line for line in path.read_text().splitlines(True) if not line.startswith('WSI+P3')]
        path.write_text(''.join(lines))
        self.assertConversionError(
            'PATIENT_ID with WSI slides not found in the clinical file: WSI+P3', study_dir=study)

    def test_sample_patient_mismatch(self):
        study = self.copy_study()
        path = study / 'data_clinical_samples.txt'
        path.write_text(path.read_text().replace('WSI-P1\tWSI-P1-S2', 'WSI-P4\tWSI-P1-S2'))
        self.assertConversionError('SAMPLE_ID WSI-P1-S2 belongs to WSI-P4 here but to WSI-P1',
                                   study_dir=study)

    def test_multiple_clinical_meta_files(self):
        study = self.copy_study()
        shutil.copy(study / 'meta_clinical_samples.txt', study / 'meta_clinical_samples_2.txt')
        self.assertConversionError('more than one SAMPLE_ATTRIBUTES clinical meta file',
                                   study_dir=study)

    def test_missing_clinical_file_fails_only_when_needed(self):
        study = self.copy_study()
        (study / 'meta_clinical_patients.txt').unlink()
        self.assertConversionError('no PATIENT_ATTRIBUTES clinical file', study_dir=study)
        # a study whose slides are all unmatched needs no sample counts
        match_level = converter.COLUMNS.index('MATCH_LEVEL')
        unmatched = [row for row in self.fixture_rows() if row.split('\t')[match_level] == 'UNMATCHED']
        study = Path(self.tmp.name) / 'study_unmatched'
        shutil.copytree(STUDY_FIXTURE_DIR, study)
        (study / 'meta_clinical_samples.txt').unlink()
        written = self.convert(meta=self.write_legacy(unmatched), study_dir=study)
        self.assertEqual(
            ['data_clinical_patients.txt', 'data_resource_definition.txt', 'data_resource_patient.txt',
             'meta_clinical_patients.txt', 'meta_resource_definition.txt', 'meta_resource_patient.txt'],
            sorted(path.name for path in written))

    def test_clinical_file_without_attribute_rows(self):
        study = self.copy_study()
        path = study / 'data_clinical_samples.txt'
        path.write_text(''.join(path.read_text().splitlines(True)[4:]))
        self.assertConversionError("expected the four '#' attribute header rows", study_dir=study)


class ConvertedFilesValidationTestCase(ConverterTestCase):

    """Run the emitted files through the real validateData validators."""

    @classmethod
    def setUpClass(cls):
        cls.portal = validateData.load_portal_info(
            'test_data/api_json_unit_tests', logging.getLogger(__name__), offline=True)

    def setUp(self):
        super().setUp()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.DEBUG)
        self.buffer = logging.handlers.BufferingHandler(capacity=1e6)
        self.logger.addHandler(self.buffer)
        self.saved = {name: getattr(validateData, name) for name in (
            'DEFINED_SAMPLE_IDS', 'PATIENTS_WITH_SAMPLES', 'SAMPLE_TO_PATIENT',
            'RESOURCE_DEFINITION_DICTIONARY', 'WSI_RESOURCE_STATE', 'DEFINED_SAMPLE_ATTRIBUTES')}
        validateData.DEFINED_SAMPLE_IDS = set(SAMPLE_TO_PATIENT)
        validateData.PATIENTS_WITH_SAMPLES = set(SAMPLE_TO_PATIENT.values())
        validateData.SAMPLE_TO_PATIENT = dict(SAMPLE_TO_PATIENT)
        validateData.DEFINED_SAMPLE_ATTRIBUTES = {'PATIENT_ID', 'SAMPLE_ID'}
        validateData.reset_wsi_resource_state()
        env = patch.dict(validateData.os.environ, NO_ALLOWLIST)
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(validateData, name, value)
        self.logger.removeHandler(self.buffer)
        super().tearDown()

    def run_validator(self, validator_class, data_file):
        validator = validator_class(str(self.out), {'data_filename': data_file}, self.portal,
                                    self.logger, False, False)
        validator.validate()
        records = list(self.buffer.buffer)
        self.buffer.flush()
        return validator, [record for record in records if record.levelno >= logging.WARNING]

    def test_resource_files_pass_validation(self):
        self.convert()
        validator, problems = self.run_validator(validateData.ResourceDefinitionValidator,
                                                 'data_resource_definition.txt')
        self.assertEqual([], [r.getMessage() for r in problems])
        validateData.RESOURCE_DEFINITION_DICTIONARY = validator.resource_definition_dictionary
        _, problems = self.run_validator(validateData.SampleResourceValidator, 'data_resource_sample.txt')
        self.assertEqual([], [(r.getMessage(), getattr(r, 'cause', None)) for r in problems])
        _, problems = self.run_validator(validateData.PatientResourceValidator, 'data_resource_patient.txt')
        self.assertEqual([], [(r.getMessage(), getattr(r, 'cause', None)) for r in problems])

    def test_count_files_pass_clinical_validation(self):
        self.convert()
        validator, problems = self.run_validator(validateData.SampleClinicalValidator,
                                                 'data_clinical_sample_wsi_counts.txt')
        self.assertEqual([], [(r.getMessage(), getattr(r, 'cause', None)) for r in problems])
        self.assertEqual({'WSI-P1-S1', 'WSI-P1-S2', 'WSI-P2-S1'}, set(validator.sampleIds))
        _, problems = self.run_validator(validateData.PatientClinicalValidator,
                                         'data_clinical_patient_wsi_counts.txt')
        # the patient-level count file is validated against the patients of all samples;
        # the only warnings are the generic ones for a patient file without survival columns
        self.assertEqual([], [(r.getMessage(), getattr(r, 'cause', None)) for r in problems
                              if 'analysis feature will not be available' not in r.getMessage()])

    def test_duplicate_image_across_resource_files_fails(self):
        self.convert()
        sample_file = self.out / 'data_resource_sample.txt'
        patient_file = self.out / 'data_resource_patient.txt'
        # give an unmatched slide the image ID of a matched one
        patient_file.write_text(patient_file.read_text().replace('"image_id":"IMG-4"', '"image_id":"IMG-1"'))
        validateData.RESOURCE_DEFINITION_DICTIONARY = {'WSI_SAMPLE': ['SAMPLE'], 'WSI_PATIENT': ['PATIENT']}
        self.run_validator(validateData.SampleResourceValidator, sample_file.name)
        _, problems = self.run_validator(validateData.PatientResourceValidator, patient_file.name)
        self.assertIn('IMAGE_ID must be unique within a study', [r.getMessage() for r in problems])

    def test_merged_study_passes_and_meta_wsi_is_rejected(self):
        study = self.copy_study()
        self.convert(study_dir=study)
        full = Path(self.tmp.name) / 'full'
        shutil.copytree(study, full)
        for path in self.out.iterdir():
            shutil.copy(path, full / path.name)
        validateData.validate_study(str(full), self.portal, self.logger, False, False)
        errors = [r for r in self.buffer.buffer if r.levelno >= logging.ERROR]
        self.buffer.flush()
        self.assertEqual([], [(r.getMessage(), getattr(r, 'cause', None)) for r in errors])

        shutil.copy(FIXTURE_DIR / 'meta_wsi.txt', full / 'meta_wsi.txt')
        shutil.copy(FIXTURE_DIR / 'data_wsi.txt', full / 'data_wsi.txt')
        validateData.validate_study(str(full), self.portal, self.logger, False, False)
        errors = [r.getMessage() for r in self.buffer.buffer if r.levelno >= logging.ERROR]
        self.buffer.flush()
        self.assertEqual([cbioportal_common.LEGACY_WSI_IMPORT_MESSAGE], errors)

if __name__ == '__main__':
    unittest.main(buffer=True)
