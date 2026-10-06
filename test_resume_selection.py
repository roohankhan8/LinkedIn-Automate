from pathlib import Path

from linkedin_search import choose_resume_path


def test_data_engineer_job_uses_data_engineer_resume():
    selected = choose_resume_path(
        {
            "title": "Data Engineer",
            "company": "Example",
            "description": "Build ETL pipelines with Python, SQL, and Airflow",
        },
        {},
    )

    assert Path(selected).name == "Roohan_Khan_Resume_DataEngineer.pdf"


if __name__ == "__main__":
    test_data_engineer_job_uses_data_engineer_resume()
