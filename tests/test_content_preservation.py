"""Content depth and skill retention survive full rewrites and safety repairs."""

import pytest

from resume_tailor.guardrails import apply_guardrails, content_preservation_issues, restore_content


BASE = """# Synthetic Candidate
candidate@example.invalid

## Professional Summary
- Python engineer supporting analytics.
- Builds reliable data services.
- Works with product teams.

## Technical Skills
- Languages: Python, R, C++, C#, SQL
- Data tools: dbt, SQL Server, CI/CD
- Platforms: AWS (EC2, S3), Azure

## Experience
### Current Systems | Jan 2024 – Present
**Data Engineer**
- Built Python services.
- Maintained batch ingestion.

### Earlier Systems | Jan 2020 – Dec 2023
**Data Engineer**
- Operated Python services.
- Maintained SQL reports.
"""


def test_fully_rewritten_points_and_additional_points_are_allowed():
    draft = (BASE.replace("Python engineer supporting analytics.", "Data engineer building AWS analytics services.")
             .replace("Built Python services.", "Implemented AWS ingestion and API workflows.")
             .replace("Maintained batch ingestion.", "Orchestrated resilient analytics pipelines.\n- Built automated data quality checks."))
    assert content_preservation_issues(BASE, draft) == []
    fixed, report = apply_guardrails(BASE, draft)
    assert report.ok, report.violations
    assert fixed == draft


def test_one_roles_extra_point_cannot_compensate_for_another_roles_missing_point():
    draft = (BASE.replace("- Maintained batch ingestion.\n", "")
             .replace("- Maintained SQL reports.", "- Maintained SQL reports.\n- Documented SQL deployments."))
    problems = content_preservation_issues(BASE, draft)
    assert len(problems) == 1
    assert "Role 1" in problems[0] and "expected at least 2, found 1" in problems[0]
    fixed, report = apply_guardrails(BASE, draft)
    assert report.ok, report.violations
    assert "- Built Python services.\n- Maintained batch ingestion.\n" in fixed
    assert any("Restored an original point" in warning and "Role 1" in warning for warning in report.warnings)


def test_summary_points_cannot_be_removed_or_combined():
    draft = BASE.replace("- Builds reliable data services.\n", "")
    problems = content_preservation_issues(BASE, draft)
    assert len(problems) == 1
    assert "professional summary" in problems[0] and "expected at least 3, found 2" in problems[0]


def test_reordered_skill_categories_lowercase_names_and_new_skills_are_allowed():
    draft = BASE.replace(
        "- Languages: Python, R, C++, C#, SQL\n- Data tools: dbt, SQL Server, CI/CD\n- Platforms: AWS (EC2, S3), Azure",
        "- Engineering: ci/cd; dbt; sql server; airflow\n- Cloud stack: azure; s3; ec2; aws\n- Programming: sql; c#; c++; r; python",
    )
    assert content_preservation_issues(BASE, draft) == []
    fixed, report = apply_guardrails(BASE, draft)
    assert report.ok, report.violations
    assert fixed == draft


@pytest.mark.parametrize("original,replacement,missing", [
    ("Python, R, C++", "Python, Rust, C++", "R"),
    ("dbt, SQL Server", "dbtCloud, SQL Server", "dbt"),
    ("C++, C#", "C, C#", "C++"),
    ("C++, C#", "C++, C", "C#"),
    ("SQL Server, CI/CD", "SQL Server, CI", "CI/CD"),
    ("dbt, SQL Server", "dbt, SQL", "SQL Server"),
    ("AWS (EC2, S3)", "AWS (EC2)", "S3"),
])
def test_skill_name_boundaries_and_grouped_children_are_retained(original, replacement, missing):
    problems = content_preservation_issues(BASE, BASE.replace(original, replacement))
    assert len(problems) == 1
    assert f"sections: {missing}." in problems[0]


def test_skill_moved_only_into_experience_still_counts_as_missing():
    draft = (BASE.replace("dbt, SQL Server", "SQL Server")
             .replace("Built Python services.", "Built Python services with dbt."))
    problems = content_preservation_issues(BASE, draft)
    assert len(problems) == 1 and "sections: dbt." in problems[0]


def test_point_dropped_by_a_timeline_repair_is_restored():
    # The invalid insertion and separate deletion are distinct diff operations:
    # dropping the added tool must not silently publish fewer experience points.
    draft = (BASE.replace("- Operated Python services.", "- Built LangGraph agents.\n- Operated Python services.")
             .replace("- Maintained SQL reports.\n", ""))
    assert content_preservation_issues(BASE, draft) == []
    fixed, report = apply_guardrails(BASE, draft)
    assert "LangGraph" not in fixed
    assert report.ok, report.violations
    assert "- Operated Python services.\n- Maintained SQL reports.\n" in fixed


def test_dropped_skill_goes_back_beside_its_original_neighbours():
    base = ("# Candidate\n\n## Technical Skills\nLanguages: Python, SQL\n"
            "AWS: S3, Glue, CodePipeline, CodeCommit, CodeBuild\nData: Snowflake, dbt\n")
    draft = base.replace("AWS: S3, Glue, CodePipeline, CodeCommit, CodeBuild",
                         "Cloud Services: S3, Glue, CodePipeline, CodeBuild, Lambda, EMR")
    fixed, notes = restore_content(base, draft)
    assert "Cloud Services: S3, Glue, CodePipeline, CodeBuild, Lambda, EMR, AWS, CodeCommit\n" in fixed
    assert notes == ["Restored skills the model removed from your skills section: AWS, CodeCommit."]
    assert content_preservation_issues(base, fixed) == []
    assert restore_content(base, fixed) == (fixed, [])


def test_merged_point_is_noted_not_repeated():
    base = BASE.replace("- Operated Python services.", "- Operated Python services for 40 teams.")
    draft = base.replace("- Operated Python services for 40 teams.\n- Maintained SQL reports.",
                         "- Operated Python services for 40 teams and maintained SQL reports.")
    fixed, notes = restore_content(base, draft)
    assert fixed == draft
    assert len(notes) == 1 and "merged" in notes[0] and "Role 2" in notes[0]
    assert content_preservation_issues(base, draft) and not content_preservation_issues(base, draft, allow_merges=True)
    _, report = apply_guardrails(base, draft)
    assert report.ok, report.violations


def test_without_skills_lines_nothing_is_guessed():
    draft = BASE.replace("- Languages: Python, R, C++, C#, SQL\n- Data tools: dbt, SQL Server, CI/CD\n"
                         "- Platforms: AWS (EC2, S3), Azure\n", "")
    fixed, notes = restore_content(BASE, draft)
    assert (fixed, notes) == (draft, [])
    assert any("Existing skills were removed" in issue for issue in content_preservation_issues(BASE, draft))


@pytest.mark.parametrize("label,missing", [("AWS", "AWS"), ("Azure Services", "Azure"),
                                           ("SQL Server", "SQL Server"), ("Azure Data Factory", "Azure Data Factory")])
def test_skills_used_as_category_labels_must_remain(label, missing):
    base = f"# Candidate\n\n## Technical Skills\n- {label}: ingestion, monitoring\n"
    draft = base.replace(f"{label}:", "Cloud tools:")
    assert any(f"sections: {missing}." in issue for issue in content_preservation_issues(base, draft))
    regrouped = draft.replace("Cloud tools: ingestion", f"Cloud tools: {missing}, ingestion")
    assert content_preservation_issues(base, regrouped) == []
