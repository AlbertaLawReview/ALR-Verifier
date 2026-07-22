from unittest import mock

import alr_quote_verifier as verifier


def test_run_audit_hands_formatted_workbook_to_final_export():
    data = {"footnote_rows": []}
    formatted = object()
    with (
        mock.patch.object(verifier, "build_verified_audit_data", return_value=data),
        mock.patch.object(verifier, "write_workbook"),
        mock.patch.object(
            verifier, "apply_cell_formatting", return_value=formatted,
        ) as apply_formatting,
        mock.patch.object(verifier, "finalize_workbook_export") as finalize,
    ):
        verifier.run_audit("input.docx", "output.xlsx")

    apply_formatting.assert_called_once_with("output.xlsx", save=False)
    finalize.assert_called_once_with(
        "output.xlsx", data, workbook=formatted,
    )


def test_final_export_uses_supplied_workbook_without_reloading():
    workbook = mock.Mock(sheetnames=[])
    workbook.properties.identifier = ""
    with mock.patch("openpyxl.load_workbook") as load_workbook:
        verifier.finalize_workbook_export(
            "output.xlsx", {"footnote_rows": []}, workbook=workbook,
        )

    load_workbook.assert_not_called()
    workbook.save.assert_called_once_with("output.xlsx")
