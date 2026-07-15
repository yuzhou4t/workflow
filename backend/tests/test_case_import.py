from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import hypoweaver.api as api_module
from hypoweaver.case_import import CaseUploadStore, DatasetRegistry, LocalCaseImporter


CSV_CONTENT = """YEAR,证券代码,SDLA,ESG,SIZE,LEV,ROA,GROWTH,unused
2019,000001.SZ,0.1,72,21.0,0.4,0.05,0.1,a
2020,000001.SZ,0.2,74,21.2,0.5,0.06,0.2,b
2020,000002.SZ,0.3,68,20.4,0.3,0.04,0.0,c
"""


class LocalCaseImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "案例1"
        self.root.mkdir()
        self.csv_path = self.root / "ESG-SDLA-数据.csv"
        self.csv_path.write_text(CSV_CONTENT, encoding="utf-8")
        (self.root / "ESG-SDLA-数据.dta").write_bytes(b"stata-alternative")
        hidden = self.root / "原始论文1"
        hidden.mkdir()
        (hidden / "paper-secret.pdf").write_bytes(b"hidden pdf conclusion")
        (hidden / "appendix-secret.docx").write_bytes(b"hidden appendix")
        (self.root / "analysis-secret.do").write_text(
            "reg SDLA ESG, robust", encoding="utf-8"
        )
        (self.root / "analysis-secret.R").write_text(
            "lm(SDLA ~ ESG)", encoding="utf-8"
        )
        self.registry_path = Path(self.tempdir.name) / "datasets.json"
        self.importer = LocalCaseImporter(DatasetRegistry(self.registry_path))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_import_infers_case_fields_hash_and_private_registry(self) -> None:
        result = self.importer.import_folder(self.root)
        case = result.case_submission
        report = result.import_report

        self.assertEqual(case.title, "企业ESG表现与短债长用")
        self.assertEqual(
            case.research_question,
            "企业 ESG 表现是否与企业短债长用程度存在系统性关联？",
        )
        self.assertEqual(case.hypotheses[0].expected_direction, "unspecified")
        roles = {variable.name: variable.role for variable in case.variables}
        self.assertEqual(roles["证券代码"], "id")
        self.assertEqual(roles["YEAR"], "time")
        self.assertEqual(roles["SDLA"], "outcome")
        self.assertEqual(roles["ESG"], "exposure")
        for control in ("SIZE", "LEV", "ROA", "GROWTH"):
            self.assertEqual(roles[control], "control")

        expected_hash = hashlib.sha256(self.csv_path.read_bytes()).hexdigest()
        self.assertEqual(case.dataset_refs[0].sha256, expected_hash)
        self.assertEqual(report.row_count, 3)
        self.assertEqual(report.column_count, 9)
        self.assertEqual((report.year_min, report.year_max), (2019, 2020))
        self.assertEqual(report.hidden_file_count, 4)
        self.assertEqual(report.excluded_file_count, 1)

        self.assertEqual(stat.S_IMODE(self.registry_path.stat().st_mode), 0o600)
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        entry = registry[report.registered_dataset_id]
        self.assertEqual(entry["source_path"], str(self.csv_path.resolve()))
        self.assertEqual(entry["sha256"], expected_hash)

    def test_hidden_names_paths_and_contents_never_leave_safe_response(self) -> None:
        result = self.importer.import_folder(self.root)
        serialized = result.model_dump_json()

        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("paper-secret", serialized)
        self.assertNotIn("appendix-secret", serialized)
        self.assertNotIn("analysis-secret", serialized)
        self.assertNotIn("hidden pdf conclusion", serialized)
        self.assertNotIn("reg SDLA ESG", serialized)
        self.assertNotIn("lm(SDLA ~ ESG)", serialized)
        self.assertNotIn("原始论文1", serialized)
        self.assertEqual(result.import_report.main_data_filename, "ESG-SDLA-数据.csv")

    def test_processed_columns_form_one_executable_default_measurement_set(self) -> None:
        self.csv_path.write_text(
            "YEAR,证券代码,SDLA,ESG,SIZE,SDLA_w,ESG_w,SIZE_w\n"
            "2019,000001.SZ,0.1,72,21.0,0.11,71,20.9\n"
            "2020,000001.SZ,0.2,74,21.2,0.19,73,21.1\n",
            encoding="utf-8",
        )

        result = self.importer.import_folder(self.root)
        variables = {variable.name: variable for variable in result.case_submission.variables}

        self.assertEqual(result.case_submission.title, "企业ESG表现与短债长用")
        self.assertEqual(
            result.case_submission.research_question,
            "企业 ESG 表现是否与企业短债长用程度存在系统性关联？",
        )
        self.assertNotIn("SDLA", variables)
        self.assertNotIn("ESG", variables)
        self.assertNotIn("SIZE", variables)
        self.assertEqual(variables["SDLA_w"].role, "outcome")
        self.assertEqual(variables["ESG_w"].role, "exposure")
        self.assertEqual(variables["SIZE_w"].role, "control")
        self.assertIn("逐行校验", variables["SDLA_w"].definition or "")
        self.assertIn("截尾", variables["SDLA_w"].definition or "")
        self.assertTrue(all(variable.definition for variable in variables.values()))
        self.assertTrue(all(variable.source for variable in variables.values()))

    def test_import_recognizes_province_panel_sd_and_gf(self) -> None:
        main_data = self.root / "main_data.csv"
        self.csv_path.unlink()
        main_data.write_text(
            "id,year,province,SD,GF,EPD\n"
            "1,2019,beijing,42.9,33.9,27.1\n"
            "1,2020,beijing,43.3,60.1,18.7\n",
            encoding="utf-8",
        )
        (self.root / "data_dictionary.csv").write_text(
            "name,definition\n" + "noise,metadata\n" * 100,
            encoding="utf-8",
        )

        result = self.importer.import_folder(self.root)
        variables = {variable.name: variable.role for variable in result.case_submission.variables}

        self.assertEqual(result.case_submission.title, "绿色金融与省级可持续发展")
        self.assertEqual(
            result.case_submission.research_question,
            "绿色金融水平是否与省级可持续发展存在系统性关联？",
        )
        self.assertEqual(variables["id"], "id")
        self.assertEqual(variables["year"], "time")
        self.assertEqual(variables["SD"], "outcome")
        self.assertEqual(variables["GF"], "exposure")
        self.assertEqual(result.import_report.main_data_filename, "main_data.csv")

    def test_import_registers_spatial_weights_as_supplementary_asset(self) -> None:
        main_data = self.root / "main_data.csv"
        self.csv_path.unlink()
        main_data.write_text(
            "id,year,province,SD,GF\n"
            "1,2019,beijing,42.9,33.9\n"
            "2,2019,tianjin,43.3,60.1\n",
            encoding="utf-8",
        )
        weights = self.root / "spatial_weights.csv"
        weights.write_text(
            "spatial_id,beijing,tianjin\n"
            "beijing,0,1\n"
            "tianjin,1,0\n",
            encoding="utf-8",
        )

        result = self.importer.import_folder(self.root)

        self.assertEqual([item.role for item in result.case_submission.dataset_refs], ["main", "supplementary"])
        self.assertEqual(result.case_submission.dataset_refs[1].filename, "spatial_weights.csv")
        self.assertEqual(result.import_report.excluded_file_count, 1)
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        self.assertIn(result.case_submission.dataset_refs[1].dataset_id, registry)


class LocalCaseImportApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "case"
        self.root.mkdir()
        (self.root / "ESG-SDLA-data.csv").write_text(CSV_CONTENT, encoding="utf-8")
        importer = LocalCaseImporter(
            DatasetRegistry(Path(self.tempdir.name) / "datasets.json")
        )
        self.importer_patch = patch.object(api_module, "case_importer", importer)
        self.importer_patch.start()
        self.upload_store_patch = patch.object(
            api_module,
            "case_upload_store",
            CaseUploadStore(Path(self.tempdir.name) / "uploads"),
        )
        self.upload_store_patch.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app),
            base_url="http://127.0.0.1",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.upload_store_patch.stop()
        self.importer_patch.stop()
        self.tempdir.cleanup()

    async def test_endpoint_uses_mutation_actor_token_guard(self) -> None:
        with patch.dict(os.environ, {"HYPOWEAVER_API_TOKEN": "test-token"}, clear=True):
            unauthorized = await self.client.post(
                "/api/v1/case-imports/local", json={"path": str(self.root)}
            )
            authorized = await self.client.post(
                "/api/v1/case-imports/local",
                json={"path": str(self.root)},
                headers={"x-hypoweaver-token": "test-token"},
            )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(authorized.status_code, 200)
        self.assertIn("case_submission", authorized.json())
        self.assertNotIn(str(self.root), authorized.text)

    async def test_csv_upload_is_persisted_and_imported_without_client_path(self) -> None:
        response = await self.client.post(
            "/api/v1/case-imports/upload",
            params={"filename": "ESG-SDLA-data.csv"},
            content=CSV_CONTENT.encode(),
            headers={"content-type": "text/csv"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["import_report"]["row_count"], 3)
        self.assertEqual(payload["case_submission"]["dataset_refs"][0]["filename"], "ESG-SDLA-data.csv")
        self.assertNotIn(str(self.tempdir.name), response.text)

        rejected = await self.client.post(
            "/api/v1/case-imports/upload",
            params={"filename": "paper.pdf"},
            content=b"not a csv",
        )
        self.assertEqual(rejected.status_code, 422)

    async def test_spatial_weights_upload_is_registered_as_supplementary(self) -> None:
        response = await self.client.post(
            "/api/v1/case-imports/assets/upload",
            params={"filename": "spatial_weights.csv"},
            content=(
                "spatial_id,beijing,tianjin\n"
                "beijing,0,1\n"
                "tianjin,1,0\n"
            ).encode(),
            headers={"content-type": "text/csv"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["role"], "supplementary")
        self.assertEqual(payload["filename"], "spatial_weights.csv")

        rejected = await self.client.post(
            "/api/v1/case-imports/assets/upload",
            params={"filename": "notes.csv"},
            content=b"name,value\nx,1\n",
        )
        self.assertEqual(rejected.status_code, 422)


if __name__ == "__main__":
    unittest.main()
