"""Dropped or reworded content goes back instead of failing the run, without inventing claims."""

import pytest

from resume_tailor import llm
from resume_tailor.guardrails import apply_guardrails, content_preservation_issues, restore_content
from tests.helpers import pack_model_output

BASE = """# Candidate
candidate@example.invalid

## Core Competencies
Data Modeling | Stakeholder Management | Agile Delivery

## Technical Skills
- Languages: Python, SQL, Scala
- Version Control: Git, CodeCommit
- Cloud: AWS, Azure

## Experience
### Example Co | Jan 2021 – Present
**Data Engineer**
- Served 2M users with Python microservices.
- Migrated 12 databases to Aurora.
- Mentored engineers on testing.
- Reduced API latency by 40% across checkout services.

### Earlier Co | Jan 2017 – Dec 2020
**Data Analyst**
- Built SQL reports for the finance team every month.
- Validated incoming data and resolved quality issues.
"""
TAILORED = BASE.replace("Mentored engineers on testing.", "Mentored engineers on automated AWS testing.")


def tailor(replies):
    calls = []

    def complete(messages, **kwargs):
        calls.append(messages)
        return pack_model_output(changelog=["Tailored"], match="SCORE: 80\ngood: fit", resume=replies[len(calls) - 1])

    try:
        return llm.tailor_resume(resume_markdown=BASE, job_description="AWS", api_key="fake", complete_fn=complete), calls
    except llm.LLMError as error:
        return error, calls


def test_emptied_skills_section_gets_the_corrective_retry():
    emptied = TAILORED.replace("Data Modeling | Stakeholder Management | Agile Delivery\n", "")
    result, calls = tailor([emptied, TAILORED])
    assert len(calls) == 2 and result.resume_markdown == TAILORED


def test_dropped_skills_line_returns_in_place_not_under_languages():
    draft = TAILORED.replace("- Version Control: Git, CodeCommit\n", "")
    fixed, notes = restore_content(BASE, draft)
    assert fixed == TAILORED
    assert notes == ["Restored skills the model removed from your skills section: Git, CodeCommit."]


def test_echo_that_drops_a_middle_skill_is_still_an_unchanged_reply():
    echo = BASE.replace("Python, SQL, Scala", "Python, Scala")
    assert restore_content(BASE, echo)[0] == BASE.replace("Python, SQL, Scala", "Python, Scala, SQL")
    result, calls = tailor([echo, TAILORED])
    assert len(calls) == 2 and "without any content changes" in calls[1][-1]["content"]


def test_echo_that_drops_a_middle_point_is_restored_in_place():
    echo = BASE.replace("- Migrated 12 databases to Aurora.\n", "")
    assert restore_content(BASE, echo)[0] == BASE


def test_merged_points_with_numbers_get_the_retry_and_never_double_a_metric():
    merged = TAILORED.replace(
        "- Served 2M users with Python microservices.\n", "").replace(
        "- Reduced API latency by 40% across checkout services.",
        "- Served 2M users with Python microservices and reduced API latency by 40% across checkout services.")
    assert restore_content(BASE, merged)[0] == merged
    result, calls = tailor([merged, TAILORED])
    assert len(calls) == 2 and result.resume_markdown == TAILORED
    # After the retry a merge is accepted and noted, never repeated.
    result, _ = tailor([merged, merged])
    assert result.resume_markdown == merged and result.resume_markdown.count("2M") == 1
    assert any("merged" in note for note in result.restored)


def test_point_moved_to_another_role_is_not_duplicated():
    moved = TAILORED.replace("- Validated incoming data and resolved quality issues.\n", "").replace(
        "- Mentored engineers", "- Validated incoming data and resolved quality issues.\n- Mentored engineers")
    fixed, notes = restore_content(BASE, moved)
    assert fixed.count("Validated incoming data") == 1 and notes == []
    assert any("Role 2" in issue for issue in content_preservation_issues(BASE, moved))


def test_skills_summary_heading_restores_each_skill_once():
    base = "# Candidate\n\n## Skills Summary\n- Languages: Python, SQL\n- Data tools: dbt, SQL Server, CI/CD\n"
    fixed, report = apply_guardrails(base, base.replace("- Data tools: dbt, SQL Server, CI/CD\n", ""))
    assert report.ok and fixed == base


@pytest.mark.parametrize("line,required", [
    ("ETL and Data Architecture: batch and real-time pipelines, data lakes and lakehouse architecture, ETL",
     {"ETL"}),
    ("Retrieval and Evaluation: RAG, embeddings, vector search", {"RAG", "embeddings"}),
    ("Quality, CI/CD, and DevOps: Git, unit testing, SCD Type 2", {"CI/CD", "Git", "SCD Type 2"}),
])
def test_descriptive_phrases_and_category_labels_are_not_required_skills(line, required):
    base = f"# Candidate\n\n## Technical Skills\n{line}\n"
    missing = content_preservation_issues(base, "# Candidate\n\n## Technical Skills\nTools: none\n")
    assert missing and set(missing[0].split(": ", 1)[1].split(". ")[0].split(", ")) == required


SWAP = """# Candidate
candidate@example.invalid

## Experience
### Example Systems, Austin | Jan 2021 – Present
**Senior Data Engineer**
- Built ingestion pipelines on AWS.
- Automated deployments with CodePipeline.

### Example Analytics, Plano | Jan 2017 – Dec 2020
**Data Analyst**
- Developed SQL reports for analysts.
- Validated incoming data before loading.
"""


def test_reworded_job_headings_are_restored_but_a_reworded_swap_is_not_relabelled():
    reworded = SWAP.replace(", Austin", "").replace(", Plano", "")
    fixed, report = apply_guardrails(SWAP, reworded)
    assert report.ok and fixed == SWAP
    first, second = reworded.index("### Example Systems"), reworded.index("### Example Analytics")
    swapped = reworded[:first] + reworded[second:] + "\n" + reworded[first:second].rstrip("\n") + "\n"
    fixed, report = apply_guardrails(SWAP, swapped)
    assert not report.ok


@pytest.mark.parametrize("header,edit", [
    ("Senior Data Engineer | jane@example.invalid | (555) 123-4567", ("jane@", "jane.doe@")),
    ("Senior Data Engineer | jane@example.invalid | (555) 123-4567", ("4567", "9999")),
    ("Senior Data Engineer | Dallas, TX\njane@example.invalid", ("Dallas, TX", "Newark, NJ")),
])
def test_a_header_line_mixing_title_and_contact_or_location_stays_frozen(header, edit):
    base = f"# Jane Sample\n{header}\n\n## Summary\n- Builds pipelines.\n"
    fixed, report = apply_guardrails(base, base.replace(*edit).replace("Senior Data", "Senior AWS Data"))
    assert report.ok and report.restored_contact and header in fixed


def test_restored_skill_is_appended_whole_never_spliced_into_another_name():
    base = "# C\n\n## Technical Skills\n- Cloud: AWS, Redshift, Kafka\n"
    fixed, _ = restore_content(base, base.replace("AWS, Redshift, Kafka", "AWS Glue, AWS Lambda, Kafka, Snowflake"))
    assert "- Cloud: AWS Glue, AWS Lambda, Kafka, Snowflake, Redshift\n" in fixed


def test_dropped_line_does_not_follow_a_substring_match_into_another_platform():
    base = "# C\n\n## Technical Skills\nAWS: S3, Step Functions\nAzure: Data Factory, ADLS, Functions, Azure DevOps\n"
    draft = "# C\n\n## Technical Skills\nAWS: S3, Step Functions, Lambda\n"
    fixed, _ = restore_content(base, draft)
    assert "AWS: S3, Step Functions, Lambda\n" in fixed
    assert "Azure: Data Factory, ADLS, Azure DevOps\n" in fixed


@pytest.mark.parametrize("line,reworded", [
    ("Big Data Technologies: Hadoop, Spark, Hive", "Distributed Processing: Hadoop, Spark, Hive"),
    ("Cloud/DevOps: AWS, Docker, Jenkins", "Cloud & DevOps: AWS, Docker, Jenkins"),
    ("Experienced with AWS Glue and Redshift for warehousing workloads.", "Delivers AWS Glue and Redshift warehousing."),
])
def test_category_labels_and_sentences_are_not_skill_names(line, reworded):
    base = f"# C\n\n## Skills Summary\n- {line}\n"
    assert content_preservation_issues(base, base.replace(line, reworded)) == []


def test_boilerplate_point_shared_by_two_roles_is_restored_where_dropped():
    common = "- Worked in an Agile team and joined daily stand-ups and sprint planning."
    base = SWAP.replace("- Automated deployments with CodePipeline.", f"- Automated deployments with CodePipeline.\n{common}") \
               .replace("- Validated incoming data before loading.", f"- Validated incoming data before loading.\n{common}")
    first = base.index(common)
    draft = base[:first] + base[first + len(common) + 1:]
    fixed, notes = restore_content(base, draft)
    assert fixed == base and len(notes) == 1


def test_restored_point_stays_under_its_own_client_sub_heading():
    base = """# C

## Experience
### Vendor Co | Jan 2018 – Present
**Data Engineer**
Client: Acme Health
- Built claims pipelines on AWS.
- Tuned Redshift queries.
Client: Globex Retail
- Built a Teradata data mart for merchandising.
- Automated weekly sales reporting.
"""
    draft = base.replace("- Built a Teradata data mart for merchandising.\n", "").replace("on AWS", "on AWS Glue")
    fixed, _ = restore_content(base, draft)
    assert "Client: Globex Retail\n- Built a Teradata data mart for merchandising.\n" in fixed


def test_hard_check_failures_get_the_corrective_retry():
    extra = TAILORED.replace("## Experience", "## Key Achievements\n- Led the AWS migration.\n\n## Experience")
    validate = lambda draft: apply_guardrails(BASE, draft)[1].violations
    calls = []

    def complete(messages, **kwargs):
        calls.append(messages)
        return pack_model_output(changelog=["x"], match="good: y", resume=[extra, TAILORED][len(calls) - 1])

    result = llm.tailor_resume(resume_markdown=BASE, job_description="AWS", api_key="fake",
                               complete_fn=complete, validate=validate)
    assert len(calls) == 2 and "Section count changed" in calls[1][-1]["content"]
    assert result.resume_markdown == TAILORED


def test_a_failed_retry_falls_back_to_the_restorable_first_reply():
    dropped = TAILORED.replace("- Migrated 12 databases to Aurora.\n", "")
    calls = []

    def complete(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 2:
            raise llm.LLMError("provider down")
        return pack_model_output(changelog=["x"], match="good: y", resume=dropped)

    result = llm.tailor_resume(resume_markdown=BASE, job_description="AWS", api_key="fake", complete_fn=complete)
    assert result.resume_markdown == TAILORED
    assert any("corrective retry failed" in note for note in result.restored)


def test_invented_client_job_is_a_hard_failure_not_a_stranded_title():
    base = SWAP.replace("### Example Systems, Austin", "### Client: Acme Health, Dallas TX").replace(
        "### Example Analytics, Plano", "### Client: Globex Retail, Austin TX")
    invented = base + "\n### Client: Initech Insurance, Remote | Jan 2014 – Dec 2016\n**ETL Developer**\n- Built invented pipelines.\n"
    fixed, report = apply_guardrails(base, invented)
    assert not report.ok and "Initech" in fixed


def test_a_moved_number_is_untangled_across_repeated_repairs():
    base = SWAP.replace("- Built ingestion pipelines on AWS.", "- Built claims pipelines for Client Northwind using Spark.") \
               .replace("- Automated deployments with CodePipeline.", "- Cut warehouse costs by 30% with Spark tuning.")
    draft = base.replace("- Built claims pipelines for Client Northwind using Spark.",
                         "- Built claims pipelines using Spark, cutting warehouse costs by 30%.") \
                .replace("- Cut warehouse costs by 30% with Spark tuning.", "- Tuned Spark jobs for warehouse workloads.")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok and fixed == base


def test_a_point_that_drops_its_own_date_range_gets_it_back():
    base = SWAP.replace("- Developed SQL reports for analysts.", "- Led the Jan 2019 – Jun 2020 Oracle to Snowflake migration.")
    draft = base.replace("the Jan 2019 – Jun 2020 Oracle", "the Oracle").replace("migration.", "migration with dbt.")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok and "Jan 2019 – Jun 2020" in fixed


PROJECTS = """# C

## Experience
### Vendor Co | Jan 2018 – Present
**Data Engineer**
Project: Claims Modernization
Responsibilities:
- Built claims ingestion pipelines on AWS.
- Tuned Redshift queries for claims analysts.
Project: Merchandising Analytics
Responsibilities:
- Built a Teradata data mart for merchandising.
- Automated weekly sales reporting with Airflow.
"""


@pytest.mark.parametrize("edit", [None, ("Project: Merchandising Analytics", "**Project: Merchandising Analytics**"),
                                  ("Project: Merchandising Analytics", "Project: Merchandising Analytics – Retail")])
def test_restored_point_never_crosses_into_another_project(edit):
    draft = PROJECTS.replace("- Built a Teradata data mart for merchandising.\n", "").replace("on AWS", "on AWS Glue")
    if edit:
        draft = draft.replace(*edit)
    fixed, _ = restore_content(PROJECTS, draft)
    claims = fixed.split("Project: Merchandising")[0]
    assert "Teradata" not in claims


@pytest.mark.parametrize("line,reworded,required", [
    ("Big Data Ecosystem: Hadoop, Spark, Hive", "Distributed Processing: Hadoop, Spark, Hive", set()),
    ("Databases: Oracle, MySQL, etc.", "Databases: Oracle, MySQL", set()),
    ("Languages – Python, SQL", "Languages: Python, SQL", set()),
    ("Reporting: Tableau (v9–v10), Secrets Manager (SM)", "Reporting: Tableau, Secrets Manager", set()),
    ("Languages: Python, SQL and Scala", "Languages: Python, SQL, Scala, PySpark", set()),
    ("Version Control: Git/GitHub, Bitbucket", "Version Control: Git, GitHub, Bitbucket", set()),
])
def test_labels_filler_versions_and_split_compounds_are_not_missing_skills(line, reworded, required):
    base = f"# C\n\n## Technical Skills\n- {line}\n"
    assert content_preservation_issues(base, base.replace(line, reworded)) == []


def test_a_dropped_point_is_restored_even_if_another_point_repeats_its_number():
    base = SWAP.replace("- Developed SQL reports for analysts.", "- Reduced Spark runtime by 40% for nightly loads.") \
               .replace("- Validated incoming data before loading.", "- Cut Snowflake costs by 40% with warehouse sizing.")
    draft = base.replace("- Cut Snowflake costs by 40% with warehouse sizing.\n", "")
    fixed, _ = restore_content(base, draft)
    assert fixed == base


def test_a_paraphrased_merge_is_noted_not_repeated():
    base = SWAP.replace("- Developed SQL reports for analysts.", "- Designed star-schema data models in Snowflake for finance reporting.") \
               .replace("- Validated incoming data before loading.", "- Automated Airflow DAGs to refresh finance dashboards daily.")
    draft = base.replace("- Designed star-schema data models in Snowflake for finance reporting.\n- Automated Airflow DAGs to refresh finance dashboards daily.",
                         "- Designed Snowflake star-schema models with Airflow-orchestrated daily refreshes for finance dashboards.")
    fixed, notes = restore_content(base, draft)
    assert fixed == draft and any("merged" in note for note in notes)


def test_long_rewritten_points_still_pair_with_their_originals():
    points = [
        "- Owned claims ingestion with PySpark and Delta Lake, converting nightly mainframe extracts into bronze, silver, "
        "and gold tables that actuaries queried directly instead of waiting for monthly spreadsheet exports from IT.",
        "- Designed Unity Catalog row-level security and column masking so underwriting, fraud, and audit groups saw only "
        "permitted policyholder attributes, replacing a fragile set of per-team views maintained by hand in the warehouse.",
        "- Rebuilt Airflow orchestration of the Snowflake loads with sensors, retries, and SLA alerts, which removed the "
        "overnight pager rotation that operations had staffed because failed jobs previously went unnoticed until morning.",
        "- Wrote Terraform modules for Databricks workspaces, cluster policies, secret scopes, and service principals so "
        "new environments came up identically in development, staging, and production without console clicks by admins.",
        "- Added Great Expectations suites to finance marts covering freshness, null rates, and reconciliation against the "
        "general ledger, publishing results to a Slack channel the controllers checked every morning before close.",
    ]
    assert all(len(point) > 200 for point in points)
    base = SWAP.replace("- Built ingestion pipelines on AWS.\n- Automated deployments with CodePipeline.", "\n".join(points))
    draft = base.replace(points[2] + "\n", "")
    for verb in ("Owned", "Designed", "Wrote", "Added"):
        draft = draft.replace(f"- {verb} ", f"- On AWS, {verb.lower()} ")
    fixed, notes = restore_content(base, draft)
    assert points[2] in fixed and not any("merged" in note for note in notes)


def test_numbers_before_a_described_unit_are_protected():
    base = SWAP.replace("- Developed SQL reports for analysts.", "- Migrated 40 legacy Informatica workflows to AWS Glue.")
    fixed, report = apply_guardrails(base, base.replace("40 legacy", "60+ legacy") + "- Built 35+ Airflow DAGs from 12 source systems.\n")
    assert report.ok and "40 legacy" in fixed and "60+" not in fixed and "35+" not in fixed


def test_a_single_renamed_section_heading_is_restored():
    base = BASE.replace("## Core Competencies", "## Professional Summary\n- Builds data platforms.\n\n## Core Competencies")
    fixed, report = apply_guardrails(base, base.replace("## Professional Summary", "## Professional Profile"))
    assert report.ok and fixed == base


@pytest.mark.parametrize("base_header,draft_header", [
    ("Senior Data Engineer\njane@example.invalid | +1 555 010 0000", "jane@example.invalid | +1 555 010 0000\nSenior AWS Data Engineer"),
    ("jane@example.invalid | +1 555 010 0000\nSenior Data Engineer", "Senior AWS Data Engineer\njane@example.invalid | +1 555 010 0000"),
])
def test_a_headline_moved_across_the_contact_line_is_kept_once(base_header, draft_header):
    base = f"# Jane Sample\n{base_header}\n\n## Summary\n- Builds pipelines.\n"
    fixed, report = apply_guardrails(base, base.replace(base_header, draft_header))
    header = fixed.split("## ")[0]
    assert report.ok and header.count("Data Engineer") == 1 and "Senior AWS Data Engineer" in header


@pytest.mark.parametrize("added", ["Newark, NJ (Hybrid) | Open to C2C", "Work Authorization: US Citizen"])
def test_an_added_location_or_authorization_line_is_removed(added):
    base = "# Jane Sample\njane@example.invalid | +1 555 010 0000\n\n## Summary\n- Builds pipelines.\n"
    fixed, report = apply_guardrails(base, base.replace("0000\n", f"0000\n{added}\n"))
    assert report.ok and report.restored_contact and added not in fixed


def test_skills_join_the_line_holding_most_of_their_neighbours_not_an_incidental_one():
    base = ("# C\n\n## Technical Skills\n- Azure: Data Factory, ADLS, Synapse Analytics\n"
            "- AI: LangGraph, LangChain, RAG, embeddings\n")
    draft = ("# C\n\n## Technical Skills\n- Orchestration: Data Factory, Airflow, Step Functions\n"
             "- Agentic AI: LangGraph, LangChain, RAG, embeddings, Synapse Analytics, conversation state\n")
    fixed, _ = restore_content(base, draft)
    assert "conversation state, ADLS" not in fixed and "ADLS" in fixed


def test_a_missing_label_word_joins_the_reworded_line_without_repeating_it():
    base = "# C\n\n## Technical Skills\n- ETL and Data Architecture: data ingestion, serverless processing, legacy migration\n"
    draft = base.replace("ETL and Data Architecture:", "Data Architecture & Streaming:")
    fixed, _ = restore_content(base, draft)
    assert fixed.count("serverless processing") == 1 and fixed.rstrip().endswith("ETL")


def test_a_summary_paraphrase_does_not_block_restoring_the_role_point():
    point = "- Migrated data from relational databases and file systems to AWS relational databases."
    base = BASE.replace("- Mentored engineers on testing.", point)
    draft = base.replace(point + "\n", "").replace("## Technical Skills", f"## Summary\n{point.replace('Migrated', 'Migrates')}\n\n## Technical Skills")
    fixed, _ = restore_content(base, draft)
    assert point in fixed


def test_retry_hears_both_the_dropped_point_and_the_failed_check():
    dropped = TAILORED.replace("- Migrated 12 databases to Aurora.\n", "").replace(
        "## Experience", "## Key Achievements\n- Led the AWS migration.\n\n## Experience")
    validate = lambda draft: apply_guardrails(BASE, draft)[1].violations
    calls = []

    def complete(messages, **kwargs):
        calls.append(messages)
        return pack_model_output(changelog=["x"], match="good: y", resume=[dropped, TAILORED][len(calls) - 1])

    llm.tailor_resume(resume_markdown=BASE, job_description="AWS", api_key="fake", complete_fn=complete, validate=validate)
    assert "Bullet count dropped" in calls[1][-1]["content"] and "Section count changed" in calls[1][-1]["content"]


def test_an_echo_that_drops_a_point_is_not_used_as_a_fallback():
    echo = BASE.replace("- Migrated 12 databases to Aurora.\n", "")
    calls = []

    def complete(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 2:
            raise llm.LLMError("provider down")
        return pack_model_output(changelog=["x"], match="good: y", resume=echo)

    with pytest.raises(llm.LLMError, match="provider down"):
        llm.tailor_resume(resume_markdown=BASE, job_description="AWS", api_key="fake", complete_fn=complete)


def test_a_rewrite_that_lost_its_number_is_reverted_in_place_not_repeated():
    metric = "- Reduced Redshift query costs by 30% through sort key and distribution tuning."
    base = SWAP.replace("- Automated deployments with CodePipeline.", metric)
    draft = base.replace(metric, "- Lowered warehouse spend on AWS by retuning Redshift DISTKEY/SORTKEY choices.\n"
                                 "- Published curated datasets to Athena for self-service reporting.")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok and fixed.count("Redshift") == 1 and metric in fixed and "Athena" in fixed


def test_similarity_of_long_lines_is_not_skewed_by_autojunk():
    from resume_tailor.guardrails import _similarity
    line = "- " + " ".join(f"built pipeline {n} with Spark, Delta Lake, and Airflow for finance" for n in range(5))
    assert len(line) > 200 and _similarity(line, line.replace("built", "delivered", 1)) > 0.9


def test_a_point_whose_sub_heading_the_draft_lost_is_not_moved_under_another_project():
    draft = PROJECTS.replace("Project: Merchandising Analytics\nResponsibilities:\n- Built a Teradata data mart for merchandising.\n", "")
    fixed, _ = restore_content(PROJECTS, draft)
    assert "Teradata" not in fixed


def test_two_incidental_names_on_a_line_are_not_a_majority():
    base = "# C\n\n## Technical Skills\n- Azure: Data Factory, ADLS, Synapse Analytics, Azure Functions, Cosmos DB\n"
    draft = ("# C\n\n## Technical Skills\n- Orchestration and Integration: Airflow, Data Factory, Azure Functions, "
             "Step Functions, EventBridge\n- Agentic AI: LangGraph, LangChain, Synapse Analytics, Cosmos DB, "
             "conversation state, tool calling\n")
    fixed, _ = restore_content(base, draft)
    assert "- Azure: ADLS\n" in fixed


HEADER = "# Ravi Sample\n{above}ravi@example.invalid | (555) 123-4567 | Dallas, TX\n{below}\n## Summary\n- Builds pipelines.\n"


@pytest.mark.parametrize("above,below,draft_above,draft_below", [
    ("Senior Data Engineer (H1B)\n", "", "Senior AWS Data Engineer (US Citizen)\n", ""),
    ("Senior Data Engineer\n", "", "Senior AWS Data Engineer (Open to Relocation)\n", ""),
    ("", "", "Senior AWS Data Engineer (US Citizen)\n", ""),
    ("", "Senior Data Engineer\n", "", "Senior AWS Data Engineer – Remote / C2C\n"),
])
def test_no_header_line_may_add_a_location_or_work_authorization(above, below, draft_above, draft_below):
    base = HEADER.format(above=above, below=below)
    fixed, report = apply_guardrails(base, HEADER.format(above=draft_above, below=draft_below))
    header = fixed.split("## ")[0]
    assert report.ok and not any(term in header for term in ("US Citizen", "Relocation", "Remote", "C2C"))
    assert (above or below).strip() in header if (above or below) else "Engineer" not in header


def test_a_retargeted_tagline_with_ai_or_ml_is_not_a_location():
    base = HEADER.format(above="", below="**Sr. AI/ML Engineer / Data Scientist**\n")
    draft = HEADER.format(above="", below="Senior AI/ML Engineer | Agentic AI, GenAI, ML\n")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok and "Agentic AI, GenAI, ML" in fixed


def test_a_sibling_merge_that_keeps_the_tool_names_is_not_repeated():
    base = SWAP.replace("- Developed SQL reports for analysts.", "- Developed SSIS workflows to load vendor files into SQL Server staging tables.") \
               .replace("- Validated incoming data before loading.", "- Scheduled recurring data processing with SQL Server Agent to support repeatable ETL execution.")
    draft = base.replace("- Developed SSIS workflows to load vendor files into SQL Server staging tables.\n- Scheduled recurring data processing with SQL Server Agent to support repeatable ETL execution.",
                         "- Built and scheduled SSIS workflows and SQL Server Agent jobs to load vendor files into staging tables for repeatable processing.")
    fixed, notes = restore_content(base, draft)
    assert fixed.count("SQL Server Agent") == 1 and any("merged" in note for note in notes)


def test_a_point_reworded_into_another_role_is_not_restored_in_both():
    base = SWAP.replace("- Automated deployments with CodePipeline.", "- Built Airflow DAGs to orchestrate daily loads into Snowflake.")
    draft = base.replace("- Built Airflow DAGs to orchestrate daily loads into Snowflake.\n", "").replace(
        "- Validated incoming data before loading.", "- Validated incoming data before loading.\n- Orchestrated daily Snowflake loads with Airflow DAGs.")
    fixed, _ = restore_content(base, draft)
    assert fixed.count("Airflow") == 1


def test_skills_follow_names_unique_to_their_line():
    base = "# C\n\n## Technical Skills\n- Languages: Python, SQL, Scala, Rust\n- Databases: SQL, Python, PostgreSQL, MySQL\n"
    draft = "# C\n\n## Technical Skills\n- Programming: Scala, Go\n- Databases: SQL, Python, PostgreSQL, MySQL\n"
    fixed, _ = restore_content(base, draft)
    assert "Rust" in fixed and "Rust" not in fixed.split("- Databases")[1]


def test_a_dropped_generic_label_does_not_block_restoring_the_point_under_its_project():
    draft = PROJECTS.replace("Project: Merchandising Analytics\nResponsibilities:\n- Built a Teradata data mart for merchandising.\n",
                             "Project: Merchandising Analytics\n")
    fixed, _ = restore_content(PROJECTS, draft)
    assert "Project: Merchandising Analytics\n- Built a Teradata data mart for merchandising.\n" in fixed


@pytest.mark.parametrize("inserted", ["**Healthcare Claims Data Platform (AWS)**", "**Louisville, KY**"])
def test_an_inserted_bold_line_under_a_job_never_becomes_a_second_title(inserted):
    draft = SWAP.replace("**Senior Data Engineer**", f"{inserted}\n**Senior AWS Data Engineer**")
    fixed, report = apply_guardrails(SWAP, draft)
    assert not report.ok and fixed.count("Senior Data Engineer") <= 1


def test_a_replaced_section_is_not_published_under_the_old_heading():
    base = SWAP + "\n## Awards\n- Employee of the Quarter, Example Co.\n"
    draft = SWAP + "\n## Selected Projects\n- Built a real-time fraud detection service on AWS Kinesis.\n"
    fixed, report = apply_guardrails(base, draft)
    assert not report.ok


def test_a_label_word_used_anywhere_in_the_resume_is_kept_and_a_vendor_prefix_is_optional():
    base = ("# C\n\n## Technical Skills\n- ETL and Data Architecture: data ingestion, CDC\n- AWS: S3, AWS Lake Formation\n\n"
            "## Experience\n### Example Co | Jan 2021 – Present\n**Data Engineer**\n- Built pipelines.\n")
    draft = base.replace("- ETL and Data Architecture: data ingestion, CDC", "- Data Architecture: data ingestion, CDC") \
                .replace("AWS Lake Formation", "Lake Formation").replace("- Built pipelines.", "- Built ETL pipelines.")
    assert content_preservation_issues(base, draft) == []
    assert restore_content(base, draft) == (draft, [])


@pytest.mark.parametrize("claim,kept", [
    ("; available for hybrid work in Newark, NJ.", False),
    (" as a US citizen.", False),
    (" across AWS and hybrid cloud environments.", True),
    (" for teams in Louisville, KY.", True),
])
def test_a_body_line_may_not_add_a_location_or_work_authorization(claim, kept):
    base = SWAP.replace("Example Analytics, Plano", "Example Analytics, Louisville, KY")
    line = "- Built ingestion pipelines on AWS."
    fixed, report = apply_guardrails(base, base.replace(line, line[:-1] + claim))
    assert report.ok and (claim in fixed) == kept
