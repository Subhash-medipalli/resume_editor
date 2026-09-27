# Tool release years

The table in `resume_tailor/guardrails.py` is a heuristic for newly introduced
tools in completed roles, not a complete historical product database. Years mean
the earliest evidenced public preview/beta, open-source release, or general
availability. Company founding, internal development, private previews, and
announcements of future availability do not qualify. Public previews may require
registration. A repository creation or arbitrary commit date alone is not proof
of a release. Compare years only; availability within the same year, in each
region, or for a particular feature is not checked. Tools already present in the
original role are exempt.

## Corrections checked on 2026-09-27

- **Spark: 2010**, not 2012. [Apache's history](https://spark.apache.org/history)
  dates open sourcing to early 2010.
- **PySpark: 2013**, not 2012. [Spark 0.7.0's announcement](https://spark.apache.org/news/)
  introduces the Python API in February 2013. Earlier development is not a release.
- **HDInsight: 2012**, not 2013. [Microsoft's October 2012 announcement](https://www.microsoft.com/en-us/sql-server/blog/2012/10/24/simplifying-big-data-for-the-enterprise/)
  makes the HDInsight previews available.
- **Pinecone: 2021**, not its 2019 founding year. [Pinecone's public-beta launch](https://www.pinecone.io/newsroom/pinecone-leaves-stealth-with-usd10m-launches-first-serverless-vector-database-for-machine-learning/)
  is dated January 27, 2021.
- **Hudi: 2017**, not internal development in 2016. [Apache's history](https://blogsarchive.apache.org/foundation/entry/the-apache-software-foundation-announces64)
  explicitly distinguishes those milestones.
- **PyTorch: 2017**, not 2016. [Meta's release history](https://engineering.fb.com/2018/12/07/ai-research/pytorch-developer-ecosystem-expands-1-0-stable-release/)
  places the first launch in early 2017.
- **Glue: 2017**, not the 2016 announcement. [AWS's December 2016 recap](https://aws.amazon.com/blogs/apn/2016-technical-recap-healthcare-and-life-sciences/)
  still describes Glue as coming soon; [the release](https://aws.amazon.com/blogs/aws/launch-aws-glue-now-generally-available/)
  and [AWS's retrospective](https://www.amazon.science/publications/the-story-of-aws-glue)
  place its launch in 2017.
- **Unity Catalog: 2022**, not the 2021 announcement. [Databricks' April 2022 release](https://www.databricks.com/blog/2022/04/20/announcing-gated-public-preview-of-unity-catalog-on-aws-and-azure.html)
  explicitly starts the gated public preview.
- **Snowpark: 2021**, not its 2020 testing environments. [Snowflake's GA announcement](https://www.snowflake.com/en/blog/snowpark-is-now-generally-available/)
  dates public preview to June 2021. This is the original Scala/Java offering;
  later language support is outside this table's granularity.
- **Delta Lake: 2018**, including its predecessor name Databricks Delta.
  [The July 2018 tutorial](https://www.databricks.com/blog/2018/07/19/simplify-streaming-stock-data-analysis-using-databricks-delta.html)
  still calls it private preview; [Microsoft's September 2018 announcement](https://azure.microsoft.com/en-us/blog/azure-databricks-delta-in-preview-9-regions-added-and-other-exciting-announcements/)
  offers the preview to customers. The Delta Lake open-source name arrived in
  [2019](https://www.databricks.com/blog/2019/04/24/open-sourcing-delta-lake.html).

## Additional source checks

These checks support the existing year; they do not establish availability of
every later feature carrying the same product name.

- **Iceberg 2017:** the project's [initial public release commit](https://github.com/apache/iceberg/commit/a5eb3f6ba171ecfc517a4f09ae9654e7d8ae0291)
  is dated December 2017; Apache incubation in 2018 is a separate milestone.
- **Trino 2020:** [the December 2020 renaming announcement](https://www.starburst.io/blog/prestosql-becomes-trino/).
- **EKS 2017:** [AWS's GA announcement](https://aws.amazon.com/blogs/aws/amazon-eks-now-generally-available/)
  dates its preceding customer preview to re:Invent 2017.
- **QuickSight 2015:** [AWS's preview announcement](https://aws.amazon.com/about-aws/whats-new/2015/10/introducing-amazon-quicksight-now-in-preview/).
- **Azure Data Factory 2014:** [Microsoft's public preview announcement](https://www.microsoft.com/en-us/sql-server/blog/2014/10/30/the-ins-and-outs-of-azure-data-factory-orchestration-and-management-of-diverse-data/).
- **Cloud Composer 2018:** [Google's 2018 index](https://cloud.google.com/blog/products/gcp/every-gcp-blog-post-2018)
  includes the May beta; [July's announcement](https://blog.google/innovation-and-ai/infrastructure-and-cloud/google-cloud/next18-recap/)
  lists general availability.
- **Kubernetes 2014:** [project history](https://kubernetes.io/blog/2018/07/20/the-history-of-kubernetes-the-community-behind-it/).
- **Terraform 2014:** [HashiCorp's release history](https://www.hashicorp.com/en/resources/the-story-of-hashicorp-terraform-with-mitchell-hashimoto).
- **GitHub Actions 2018:** [GitHub's workshop materials](https://github.com/githubuniverseworkshops/building-blocks)
  date the beta to 2018.
- **FAISS 2017:** [Meta's 2018 retrospective](https://engineering.fb.com/2018/12/05/ai-research/fair-fifth-anniversary/)
  says it was open-sourced the previous year.
- **AutoGen 2023:** [Microsoft's announcement](https://www.microsoft.com/en-us/research/?p=969759).
- **LangGraph 2024:** [LangChain's January announcement](https://blog.langchain.dev/langgraph/).
- **Model Context Protocol 2024:** [Anthropic's November announcement](https://www.anthropic.com/news/model-context-protocol).
- **Agent Bricks 2025:** [Databricks' June beta announcement](https://www.prnewswire.com/in/news-releases/databricks-launches-agent-bricks-a-new-approach-to-building-ai-agents-302478860.html).

## Audit limits

**Lake Formation 2018** remains a conservative, uncertain value: [AWS's November
2018 announcement](https://aws.amazon.com/about-aws/whats-new/2018/11/announcing-aws-lake-formation/)
offers preview registration, but this check did not establish when registrants
first received access. Do not describe that year as independently verified.

The remaining original entries have not been independently source-verified in
this bounded review: EMR, RDS, CloudFormation, DynamoDB, Redshift, Kinesis, Lambda,
Aurora, KMS, ECS, API Gateway, Athena, Step Functions, SageMaker, Fargate, Secrets
Manager, MSK, EventBridge, OpenSearch, Bedrock, Event Hubs, ADLS, Azure Functions,
Cosmos DB, Azure Databricks, AKS, Azure DevOps, Synapse, Azure OpenAI, Microsoft
Fabric, Azure AI Foundry, BigQuery, Dataproc, Vertex AI, Kafka, Presto, Databricks,
Snowflake, Flink, NiFi, Airflow, dbt, Great Expectations, Airbyte, Delta Live Tables,
Databricks Genie, Docker, TensorFlow, MLflow, RAG, ChatGPT, LangChain, LlamaIndex,
GPT-4, and CrewAI. Preserve a primary-source link here when correcting or adding
an entry, and distinguish public availability from announcement and development.
