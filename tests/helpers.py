from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURE_RESUME_PATH = FIXTURE_DIR / "synthetic_resume.md"
FIXTURE_JD_PATH = FIXTURE_DIR / "synthetic_jd.txt"
PIPE_RESUME_PATH = FIXTURE_DIR / "synthetic_pipe_resume.md"

SAMPLE_RESUME = FIXTURE_RESUME_PATH.read_text(encoding="utf-8")
SAMPLE_JD = FIXTURE_JD_PATH.read_text(encoding="utf-8")
PIPE_RESUME = PIPE_RESUME_PATH.read_text(encoding="utf-8")


def pack_model_output(*, changelog: list[str], match: str, resume: str) -> str:
    bullets = "\n".join(f"- {item}" for item in changelog)
    return (
        "===CHANGELOG===\n"
        f"{bullets}\n"
        "===MATCH===\n"
        f"{match}\n"
        "===RESUME===\n"
        f"{resume.rstrip()}\n"
    )


def lightly_tailored(base: str) -> str:
    """A realistic small edit of the synthetic resume — no new employers/dates/metrics."""
    return (
        base.replace(
            "Contract software engineer focused on backend services and cloud platform work. "
            "Comfortable joining mid-engagement, shipping in Python, and supporting AWS-hosted systems.",
            "Contract software engineer focused on backend services and AWS-hosted platform work. "
            "Comfortable joining a scoped mid-engagement, shipping Python REST APIs, and supporting "
            "EC2/S3/IAM/CloudWatch systems.",
        ).replace(
            "- Built and operated Python services that ingest partner events and expose REST APIs for internal tools.",
            "- Built and operated Python services that ingest partner events and expose REST APIs, aligned to scoped platform work.",
        )
    )
