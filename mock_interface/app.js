// ─── Routing constants (mirrors TaskRoutingAgent) ─────────────────────────────

const RAG_KEYWORDS = [
  "algorithm", "context", "describe", "docs", "document", "documentation",
  "explain", "hurricane", "impact", "knowledge", "method", "ncei", "nhc",
  "prompt", "rag", "reference", "steps", "summarize",
];
const RAG_PHRASES = ["how does", "how do"];
const SIMILARITY_KEYWORDS = ["similar", "find similar", "similar to", "rank storms"];
const ANALYTICS_KEYWORDS = [
  "analyze", "analysis", "predict", "outage", "storm", "weather", "compare",
  "trend", "top", "county", "counties",
];
const DATA_KEYWORDS = [
  "load", "retrieve", "raw", "csv", "schema", "table", "database", "db",
  "sqlite", "ingest", "normalize",
];
const CT_COUNTIES = [
  "fairfield", "hartford", "litchfield", "middlesex",
  "new haven", "new london", "tolland", "windham",
];

// ─── Router ───────────────────────────────────────────────────────────────────

function routeQuery(query) {
  const text = query.toLowerCase();
  const numericMatches = [
    ...query.matchAll(/\b(wind|precip|wgt5|wgt9|wgt13|wgt18)\s*=\s*([\d.]+)/gi),
  ];
  const hasNumericParam = numericMatches.length > 0;

  const ragKw = RAG_KEYWORDS.filter((kw) => text.includes(kw));
  const ragPh = RAG_PHRASES.filter((ph) => text.includes(ph));
  if ((ragKw.length > 0 || ragPh.length > 0) && !hasNumericParam) {
    return {
      route: "rag_knowledge",
      reason: "Matched documentation/RAG knowledge keywords.",
      keywords: [...ragKw, ...ragPh],
    };
  }

  const simKw = SIMILARITY_KEYWORDS.filter((kw) => text.includes(kw));
  if (simKw.length > 0 || hasNumericParam) {
    const paramStrings = numericMatches.map((m) => `${m[1]}=${m[2]}`);
    return {
      route: "similarity_search",
      reason: "Matched similarity keywords or numeric parameters.",
      keywords: [...simKw, ...paramStrings],
    };
  }

  const analyticsKw = ANALYTICS_KEYWORDS.filter((kw) => text.includes(kw));
  if (analyticsKw.length > 0) {
    return {
      route: "weather_analytics",
      reason: "Matched weather/outage analytics keywords.",
      keywords: analyticsKw,
    };
  }

  const dataKw = DATA_KEYWORDS.filter((kw) => text.includes(kw));
  if (dataKw.length > 0) {
    return {
      route: "data_retrieval",
      reason: "Matched data/database-oriented keywords.",
      keywords: dataKw,
    };
  }

  return {
    route: "unknown",
    reason: "No keyword match. Try 'similar', 'outage', 'explain', 'csv', or numeric params like wind=9.2.",
    keywords: [],
  };
}

// ─── Mock data ────────────────────────────────────────────────────────────────

const MOCK_SIM = {
  fairfield: {
    wind: [
      { code: "2020022700", val: 9.202, outage: 51, date: "2020-02-27", sim: 99.9 },
      { code: "2016032818", val: 9.205, outage: 124, date: "2016-03-28", sim: 99.7 },
      { code: "2023033118", val: 9.217, outage: 15, date: "2023-03-31", sim: 99.0 },
      { code: "2018020600", val: 9.240, outage: 88, date: "2018-02-06", sim: 97.8 },
      { code: "2011101200", val: 9.310, outage: 203, date: "2011-10-12", sim: 95.2 },
    ],
    precip: [
      { code: "2012102900", val: 78.3, outage: 12840, date: "2012-10-29", sim: 99.2 },
      { code: "2011082900", val: 64.1, outage: 8921, date: "2011-08-29", sim: 97.8 },
      { code: "2018103000", val: 41.5, outage: 5432, date: "2018-10-30", sim: 95.1 },
      { code: "2017030800", val: 48.2, outage: 2341, date: "2017-03-08", sim: 93.7 },
    ],
    wgt: [
      { code: "2012102900", val: "wgt5=11 wgt9=6", outage: 12840, date: "2012-10-29", sim: 98.3 },
      { code: "2011082900", val: "wgt5=9 wgt9=4", outage: 8921, date: "2011-08-29", sim: 96.1 },
      { code: "2020082100", val: "wgt5=7 wgt9=3", outage: 3201, date: "2020-08-21", sim: 93.4 },
    ],
  },
  "new haven": {
    wind: [
      { code: "2012102900", val: 12.48, outage: 4102, date: "2012-10-29", sim: 99.8 },
      { code: "2018030200", val: 12.51, outage: 891, date: "2018-03-02", sim: 99.5 },
      { code: "2021012800", val: 12.55, outage: 432, date: "2021-01-28", sim: 98.7 },
      { code: "2020082100", val: 12.63, outage: 174, date: "2020-08-21", sim: 96.3 },
      { code: "2015022700", val: 12.71, outage: 27, date: "2015-02-27", sim: 93.1 },
    ],
    precip: [
      { code: "2012102900", val: 45.1, outage: 4102, date: "2012-10-29", sim: 99.6 },
      { code: "2011082900", val: 45.8, outage: 2014, date: "2011-08-29", sim: 98.9 },
      { code: "2018030200", val: 43.2, outage: 891, date: "2018-03-02", sim: 97.4 },
    ],
    wgt: [
      { code: "2012102900", val: "wgt5=14 wgt9=8", outage: 4102, date: "2012-10-29", sim: 99.1 },
      { code: "2011082900", val: "wgt5=10 wgt9=5", outage: 2014, date: "2011-08-29", sim: 95.7 },
      { code: "2018030200", val: "wgt5=9 wgt9=4", outage: 891, date: "2018-03-02", sim: 92.3 },
    ],
  },
  hartford: {
    wind: [
      { code: "2011082800", val: 10.87, outage: 3241, date: "2011-08-28", sim: 99.4 },
      { code: "2018102800", val: 10.91, outage: 1882, date: "2018-10-28", sim: 98.9 },
      { code: "2020033100", val: 10.95, outage: 782, date: "2020-03-31", sim: 97.2 },
    ],
    precip: [
      { code: "2011082800", val: 54.2, outage: 3241, date: "2011-08-28", sim: 98.7 },
      { code: "2018102800", val: 38.1, outage: 1882, date: "2018-10-28", sim: 96.4 },
      { code: "2020033100", val: 22.5, outage: 782, date: "2020-03-31", sim: 93.1 },
    ],
    wgt: [
      { code: "2011082800", val: "wgt5=8 wgt9=3", outage: 3241, date: "2011-08-28", sim: 99.2 },
      { code: "2018102800", val: "wgt5=7 wgt9=3", outage: 1882, date: "2018-10-28", sim: 97.8 },
      { code: "2020033100", val: "wgt5=8 wgt9=2", outage: 782, date: "2020-03-31", sim: 95.3 },
    ],
  },
};

const MOCK_ANALYTICS = {
  topCounties: [
    { county: "Fairfield", storms: 294, total: 43475, peak: 12840 },
    { county: "Hartford", storms: 294, total: 23307, peak: 8921 },
    { county: "Litchfield", storms: 294, total: 17601, peak: 5432 },
    { county: "New Haven", storms: 294, total: 14889, peak: 4102 },
    { county: "Tolland", storms: 294, total: 9341, peak: 3211 },
  ],
  topStorms: [
    { code: 20121029, date: "2012-10-29", length: 48, outage: 27834 },
    { code: 20110829, date: "2011-08-29", length: 36, outage: 15402 },
    { code: 20181030, date: "2018-10-30", length: 24, outage: 9871 },
    { code: 20200810, date: "2020-08-10", length: 12, outage: 6342 },
    { code: 20170312, date: "2017-03-12", length: 30, outage: 4987 },
  ],
  history: {
    fairfield: [
      { code: 20121029, date: "2012-10-29", precip: 78.3, wind: 18.4, outage: 12840 },
      { code: 20110829, date: "2011-08-29", precip: 64.1, wind: 15.2, outage: 8921 },
      { code: 20181030, date: "2018-10-30", precip: 41.5, wind: 12.7, outage: 5432 },
      { code: 20200810, date: "2020-08-10", precip: 28.2, wind: 10.1, outage: 3201 },
      { code: 20230331, date: "2023-03-31", precip: 15.6, wind: 9.2, outage: 1547 },
    ],
    "new haven": [
      { code: 20121029, date: "2012-10-29", precip: 45.1, wind: 12.5, outage: 4102 },
      { code: 20110829, date: "2011-08-29", precip: 38.2, wind: 11.3, outage: 2014 },
      { code: 20180302, date: "2018-03-02", precip: 22.4, wind: 12.5, outage: 891 },
      { code: 20210128, date: "2021-01-28", precip: 18.1, wind: 12.6, outage: 432 },
    ],
    hartford: [
      { code: 20110828, date: "2011-08-28", precip: 54.2, wind: 11.2, outage: 3241 },
      { code: 20181028, date: "2018-10-28", precip: 38.1, wind: 10.9, outage: 1882 },
      { code: 20200331, date: "2020-03-31", precip: 22.5, wind: 10.9, outage: 782 },
    ],
    _default: [
      { code: 20121029, date: "2012-10-29", precip: 72.1, wind: 16.3, outage: 9231 },
      { code: 20110829, date: "2011-08-29", precip: 58.4, wind: 13.7, outage: 5104 },
      { code: 20181030, date: "2018-10-30", precip: 39.2, wind: 11.8, outage: 2891 },
    ],
  },
};

// ─── Output generators ────────────────────────────────────────────────────────

function genRag(query, text) {
  if (text.includes("hurricane") || text.includes("irene") || text.includes("sandy")) {
    return {
      title: "Hurricane impact summary",
      mode: "External context + RAG",
      bullets: [
        "Hurricane Irene (Aug 2011) caused major flooding and power outages across all 8 Connecticut counties. [1]",
        "NCEI records show Irene produced peak wind speeds above 15 m/s in Fairfield and New Haven counties. [2]",
        "Superstorm Sandy (Oct 2012) drove the highest recorded outage counts with 27,834 statewide. [3]",
        "NHC tropical cyclone reports are indexed as RAG sources and retrieved as supporting context. [1]",
      ],
      sources: [
        { label: "NCEI CT Billion-Dollar Weather Events", score: 0.94 },
        { label: "NHC Tropical Cyclone Report — Hurricane Irene", score: 0.89 },
        { label: "NHC Tropical Cyclone Report — Superstorm Sandy", score: 0.84 },
      ],
    };
  }
  if (text.includes("algorithm") || (text.includes("explain") && text.includes("similar"))) {
    return {
      title: "Similarity algorithm explanation",
      mode: "Local RAG",
      bullets: [
        "WISER standardizes each feature to Z-scores (mean=0, std=1) across the county's historical dataset. [1]",
        "Distance is the weighted L2 norm: sqrt(Σ w·(z_hist − z_query)²). [1]",
        "Similarity = 1/(1+distance), giving a score in (0, 1] where 1.0 is a perfect match. [2]",
        "Up to 6 features can be combined: peakWSPD10, sumAFWA_TOTPRECIP, wgt5, wgt9, wgt13, wgt18. [2]",
      ],
      sources: [
        { label: "docs/STORM_SIMILARITY_ALGORITHM.md", score: 0.96 },
        { label: "src/wiser/similarity_engine.py", score: 0.88 },
        { label: "src/wiser/agents/similarity_query_agent.py", score: 0.73 },
      ],
    };
  }
  if (text.includes("explain") || text.includes("how does") || text.includes("how do")) {
    return {
      title: "System explanation",
      mode: "Local RAG",
      bullets: [
        "WISER routes natural language queries to one of four agents via TaskRoutingAgent keyword matching. [1]",
        "The RAG agent indexes .md/.py/.sql/.txt files using TF-IDF with title (3×) and path (2×) boosts. [2]",
        "Similarity uses weighted Z-score distance across up to 6 weather features. [1]",
        "Analytics runs three hardcoded SQL queries: top counties, top storms, county history. [2]",
      ],
      sources: [
        { label: "README.md", score: 0.91 },
        { label: "src/wiser/agents/task_routing_agent.py", score: 0.85 },
        { label: "src/wiser/rag.py", score: 0.79 },
      ],
    };
  }
  if (text.includes("summarize") || text.includes("impact") || text.includes("ncei") || text.includes("nhc")) {
    return {
      title: "Storm impact summary",
      mode: "External context",
      bullets: [
        "NOAA/NCEI and NHC external sources are fetched and chunked into the RAG knowledge base. [1]",
        "Lexical TF-IDF retrieval surfaces the most relevant document chunks for the query. [2]",
        "Optional OpenAI (gpt-5.5) generates grounded bullet-point summaries from retrieved context. [1]",
        "Local extractive fallback is used when no OPENAI_API_KEY is configured. [2]",
      ],
      sources: [
        { label: "NCEI Connecticut Weather & Climate Disasters", score: 0.91 },
        { label: "docs/wiser_external_sources.md", score: 0.84 },
        { label: "src/wiser/openai_summarizer.py", score: 0.72 },
      ],
    };
  }
  if (text.includes("rag") || text.includes("knowledge") || text.includes("docs") || text.includes("document")) {
    return {
      title: "RAG knowledge system",
      mode: "Local RAG",
      bullets: [
        "The RAG agent indexes .md, .py, .sql, and .txt files under docs/, scripts/, and src/wiser/. [1]",
        "Documents are chunked into ~180-token windows with 30-token overlap. [1]",
        "TF-IDF scoring boosts title matches (3×) and path matches (2×) over body content. [2]",
        "Answers are grounded: each bullet cites a source chunk from the index. [2]",
      ],
      sources: [
        { label: "src/wiser/rag.py", score: 0.93 },
        { label: "src/wiser/agents/rag_knowledge_agent.py", score: 0.87 },
        { label: "README.md#rag-knowledge-agent", score: 0.74 },
      ],
    };
  }
  return {
    title: "Knowledge answer",
    mode: "Local RAG",
    bullets: [
      "The RAG agent retrieved relevant document chunks from the WISER knowledge base. [1]",
      "Lexical TF-IDF search matched query terms across indexed source files. [2]",
      "A grounded extractive answer was synthesized from the top-ranked chunks. [1]",
    ],
    sources: [
      { label: "README.md", score: 0.82 },
      { label: "docs/", score: 0.74 },
      { label: "src/wiser/rag.py", score: 0.66 },
    ],
  };
}

function genSimilarity(query) {
  const text = query.toLowerCase();
  const county = CT_COUNTIES.find((c) => text.includes(c)) || "fairfield";
  const numericMatches = [
    ...query.matchAll(/\b(wind|precip|wgt5|wgt9|wgt13|wgt18)\s*=\s*([\d.]+)/gi),
  ];
  const params = {};
  numericMatches.forEach((m) => {
    params[m[1].toLowerCase()] = parseFloat(m[2]);
  });

  const isWgt = "wgt5" in params || "wgt9" in params || "wgt13" in params || "wgt18" in params;
  const isPrecipOnly = "precip" in params && !("wind" in params) && !isWgt;
  const paramStr = numericMatches.map((m) => `${m[1]}=${m[2]}`).join(", ") || "wind=9.2";
  const countyLabel = county.split(" ").map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");

  const countyData = MOCK_SIM[county] || MOCK_SIM.fairfield;
  const rows = isWgt ? countyData.wgt : isPrecipOnly ? countyData.precip : countyData.wind;
  const featureLabel = isWgt
    ? "wgt5 / wgt9 (exceedance hours)"
    : isPrecipOnly
    ? "precip (sumAFWA_TOTPRECIP mm)"
    : "wind speed (peakWSPD10 m/s)";

  return {
    title: `Similar storms — ${countyLabel}`,
    mode: "Z-score similarity",
    bullets: [
      `Searching ${countyLabel} county historical records with: ${paramStr}.`,
      `Primary feature: ${featureLabel}. Z-scores computed across all county storms.`,
      `Top match: ${rows[0]?.sim ?? 99}% similarity via weighted L2 Z-score distance.`,
    ],
    simRows: (rows || []).slice(0, 5),
    isWgt,
    isPrecipOnly,
    paramStr,
    countyLabel,
    params,
  };
}

function genAnalytics(query, text) {
  const county = CT_COUNTIES.find((c) => text.includes(c));
  const wantsHistory =
    county && (text.includes("history") || text.includes("show") || text.includes("for"));
  const wantsTopStorms =
    (text.includes("storm") || text.includes("storms")) &&
    (text.includes("top") || text.includes("worst") || text.includes("most"));

  if (wantsHistory && county) {
    const countyLabel = county.split(" ").map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");
    return {
      title: `Storm history — ${countyLabel}`,
      mode: "SQLite analytics",
      bullets: [
        `${countyLabel} county has 294 recorded storm events in the WISER database.`,
        "Results ordered by descending total outage count.",
        "Columns: stormCode, date, precipitation (mm), peak wind (m/s), outages.",
      ],
      analyticsType: "history",
      county,
      countyLabel,
      sql: `SELECT w.stormCode, s.eventDateTime,\n  w.sumAFWA_TOTPRECIP, w.peakWSPD10, w.outage\nFROM WeatherData w\nJOIN Storm s ON s.stormCode = w.stormCode\nWHERE LOWER(w.county) = '${county}'\nORDER BY w.outage DESC\nLIMIT 10`,
    };
  }
  if (wantsTopStorms) {
    return {
      title: "Top storms by total outage",
      mode: "SQLite analytics",
      bullets: [
        "Superstorm Sandy (2012-10-29) ranks first with 27,834 total outages statewide.",
        "Hurricane Irene (2011-08-29) ranks second with 15,402 outages.",
        "Results aggregated across all 8 counties via SQL JOIN + GROUP BY stormCode.",
      ],
      analyticsType: "topStorms",
      sql: "SELECT s.stormCode, s.eventDateTime,\n  s.eventLength, SUM(w.outage) AS total_outage\nFROM Storm s\nJOIN WeatherData w ON w.stormCode = s.stormCode\nGROUP BY s.stormCode\nORDER BY total_outage DESC\nLIMIT 10",
    };
  }
  return {
    title: "Top counties by total outage",
    mode: "SQLite analytics",
    bullets: [
      "Fairfield ranks first with 43,475 total outages across 294 storms.",
      "Hartford ranks second with 23,307 total outages.",
      "Results from: SELECT county, SUM(outage) … GROUP BY county ORDER BY total DESC.",
    ],
    analyticsType: "topCounties",
    sql: "SELECT county,\n  COUNT(*) AS storms_observed,\n  SUM(outage) AS total_outage,\n  MAX(outage) AS peak_outage\nFROM WeatherData\nGROUP BY county\nORDER BY total_outage DESC\nLIMIT 10",
  };
}

function genData(query, text) {
  if (text.includes("normalize") || text.includes("ingest")) {
    return {
      title: "Normalize & ingest pipeline",
      mode: "Data processing",
      bullets: [
        "DataRetrievalProcessingAgent reads and normalizes the raw storm CSV.",
        "Rows are split into Storm (294), County (8), and WeatherData (2,352) relational tables.",
        "Normalized CSVs are written to output/ and loaded into SQLite via initialize_schema().",
      ],
    };
  }
  if (text.includes("schema") || text.includes("table")) {
    return {
      title: "Database schema",
      mode: "Data processing",
      bullets: [
        "Storm table: stormCode (PK), eventDateTime, eventLength.",
        "County table: county (PK), state.",
        "WeatherData: FK to Storm + County, peakWSPD10, sumAFWA_TOTPRECIP, wgt5/9/13/18, outage.",
      ],
    };
  }
  return {
    title: "Database build workflow",
    mode: "Data processing",
    bullets: [
      "DataRetrievalProcessingAgent normalizes the raw storm CSV into 3 relational tables.",
      "Builds Storm (294), County (8), and WeatherData (2,352) rows in SQLite.",
      "Run: python3 scripts/bootstrap_wiser.py to execute the full pipeline.",
    ],
  };
}

function generateOutput(query, decision) {
  const text = query.toLowerCase();
  switch (decision.route) {
    case "rag_knowledge": return genRag(query, text);
    case "similarity_search": return genSimilarity(query);
    case "weather_analytics": return genAnalytics(query, text);
    case "data_retrieval": return genData(query, text);
    default:
      return {
        title: "No route matched",
        mode: "Unknown",
        bullets: [
          "No strong keyword signal detected in this query.",
          "Try 'similar', 'outage', 'explain', 'csv', 'hurricane', or numeric params like wind=9.2.",
          "The router requires at least one keyword to assign the query to an agent.",
        ],
      };
  }
}

// ─── DOM refs ─────────────────────────────────────────────────────────────────

const queryInput = document.querySelector("#query-input");
const routePill = document.querySelector("#route-pill");
const modePill = document.querySelector("#mode-pill");
const resultTitle = document.querySelector("#result-title");
const answerList = document.querySelector("#answer-list");
const queryForm = document.querySelector("#query-form");
const routerQueryText = document.querySelector("#router-query-text");
const routerReason = document.querySelector("#router-reason");
const routerSelected = document.querySelector("#router-selected");
const routerKeywords = document.querySelector("#router-keywords");
const routeCards = document.querySelectorAll(".route-card");
const simPanel = document.querySelector("#similarity");
const evidencePanel = document.querySelector("#evidence");
const barList = document.querySelector("#bar-list");
const simTable = document.querySelector("#sim-table");
const simLabel = document.querySelector("#sim-label");
const simTitle = document.querySelector("#sim-title");
const simBadge = document.querySelector("#sim-badge");
const evidenceTitle = document.querySelector("#evidence-title");
const sourcesList = document.querySelector("#sources-list");

// ─── Pill color map ───────────────────────────────────────────────────────────

const PILL_CLASS = {
  rag_knowledge: "pill-teal",
  similarity_search: "pill-blue",
  weather_analytics: "pill-orange",
  data_retrieval: "pill-green",
  unknown: "pill-red",
};

// ─── Panel renderers ──────────────────────────────────────────────────────────

function sourceRow(index, label, badge, first) {
  return `<div class="source-row${first ? " source-first" : ""}">
    <span>${index}</span><p>${label}</p><b>${badge}</b>
  </div>`;
}

function renderSimPanel(output) {
  simPanel.classList.remove("dimmed");
  simLabel.textContent = "Similarity engine";
  simTitle.textContent = `${output.countyLabel} · ${output.paramStr}`;
  simBadge.textContent = "Multi-dimensional Z-score";

  const rows = output.simRows || [];
  const isWgt = output.isWgt;
  const isPrecip = output.isPrecipOnly;
  const valHeader = isWgt ? "Features matched" : isPrecip ? "Precip (mm)" : "Wind (m/s)";

  barList.innerHTML = rows.slice(0, 3).map((r) => `
    <div class="bar-row">
      <b>${r.code}</b>
      <div><span style="width:${r.sim}%"></span></div>
      <em>${r.sim}%</em>
    </div>`).join("");

  simTable.innerHTML = `
    <thead><tr><th>Rank</th><th>${valHeader}</th><th>Outages</th><th>Date</th></tr></thead>
    <tbody>${rows.map((r, i) => `
      <tr>
        <td>${i + 1}</td>
        <td>${r.val}</td>
        <td>${r.outage.toLocaleString()}</td>
        <td>${r.date}</td>
      </tr>`).join("")}
    </tbody>`;
}

function renderAnalyticsPanel(output) {
  simPanel.classList.remove("dimmed");
  simLabel.textContent = "Analytics results";
  simTitle.textContent = output.title;
  simBadge.textContent = "SQLite";
  barList.innerHTML = "";

  if (output.analyticsType === "topCounties") {
    simTable.innerHTML = `
      <thead><tr><th>County</th><th>Storms</th><th>Total Outages</th><th>Peak</th></tr></thead>
      <tbody>${MOCK_ANALYTICS.topCounties.map((r) => `
        <tr>
          <td>${r.county}</td><td>${r.storms}</td>
          <td>${r.total.toLocaleString()}</td>
          <td>${r.peak.toLocaleString()}</td>
        </tr>`).join("")}
      </tbody>`;
  } else if (output.analyticsType === "topStorms") {
    simTable.innerHTML = `
      <thead><tr><th>Storm Code</th><th>Date</th><th>Duration (h)</th><th>Total Outages</th></tr></thead>
      <tbody>${MOCK_ANALYTICS.topStorms.map((r) => `
        <tr>
          <td>${r.code}</td><td>${r.date}</td>
          <td>${r.length}</td><td>${r.outage.toLocaleString()}</td>
        </tr>`).join("")}
      </tbody>`;
  } else if (output.analyticsType === "history") {
    const rows =
      MOCK_ANALYTICS.history[output.county] || MOCK_ANALYTICS.history._default;
    simTable.innerHTML = `
      <thead><tr><th>Code</th><th>Date</th><th>Precip (mm)</th><th>Wind (m/s)</th><th>Outages</th></tr></thead>
      <tbody>${rows.map((r) => `
        <tr>
          <td>${r.code}</td><td>${r.date}</td>
          <td>${r.precip}</td><td>${r.wind}</td>
          <td>${r.outage.toLocaleString()}</td>
        </tr>`).join("")}
      </tbody>`;
  }
}

function dimSimPanel() {
  simPanel.classList.add("dimmed");
}

function renderEvidenceRag(output) {
  evidencePanel.classList.remove("dimmed");
  evidenceTitle.textContent = "Retrieved sources";
  const srcs = output.sources || [];
  sourcesList.innerHTML = srcs
    .map((s, i) => sourceRow(i + 1, s.label, s.score, i === 0))
    .join("");
}

function renderEvidenceSimilarity(output) {
  evidencePanel.classList.remove("dimmed");
  evidenceTitle.textContent = "Query features";
  const entries = Object.entries(output.params || {});
  const rows = [
    ...entries.map(([k, v], i) =>
      sourceRow(
        "↗",
        `<strong>${k}</strong> = ${v} &nbsp;→&nbsp; Z-scored against ${output.countyLabel} history`,
        "wt 1.0",
        i === 0,
      )
    ),
    sourceRow(
      "↗",
      `Historical rows fetched: WeatherData WHERE county = '${output.countyLabel}'`,
      "≤1000",
      entries.length === 0,
    ),
  ];
  sourcesList.innerHTML = rows.join("");
}

function renderEvidenceAnalytics(output) {
  evidencePanel.classList.remove("dimmed");
  evidenceTitle.textContent = "SQL executed";
  const sql = output.sql || "";
  sourcesList.innerHTML = `
    <div style="padding:12px;background:#15202b;border-radius:8px;margin-top:2px;">
      <p style="margin:0 0 8px;color:#8899aa;font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;">query_engine.py</p>
      <pre style="margin:0;color:#d8f3dc;font-family:monospace;font-size:12px;line-height:1.65;white-space:pre-wrap;">${sql}</pre>
    </div>`;
}

function renderEvidenceData() {
  evidencePanel.classList.remove("dimmed");
  evidenceTitle.textContent = "Pipeline stages";
  sourcesList.innerHTML = [
    sourceRow(1, "load_raw_csv() — read raw storm CSV from disk", "✓", true),
    sourceRow(2, "normalize_rows() — split into Storm / County / WeatherData", "✓", false),
    sourceRow(3, "initialize_schema() — create SQLite tables", "✓", false),
    sourceRow(4, "load_normalized_rows() — insert 2,352 rows", "✓", false),
  ].join("");
}

function renderEvidenceUnknown() {
  evidencePanel.classList.remove("dimmed");
  evidenceTitle.textContent = "No match";
  sourcesList.innerHTML = sourceRow(
    "?",
    "No keyword match — query could not be routed to an agent.",
    "—",
    true,
  );
}

// ─── Main render ──────────────────────────────────────────────────────────────

function renderQuery(query) {
  query = query.trim();
  if (!query) return;

  const decision = routeQuery(query);
  const output = generateOutput(query, decision);

  // Route pill
  routePill.textContent = decision.route;
  routePill.className = PILL_CLASS[decision.route] || "pill-teal";

  // Router section
  routerQueryText.textContent = query;
  routerReason.textContent = decision.reason;
  routerSelected.textContent = decision.route;
  routerKeywords.textContent = decision.keywords.join(", ") || "—";
  routeCards.forEach((card) => {
    card.classList.toggle("active", card.dataset.route === decision.route);
  });

  // Response card
  modePill.textContent = output.mode;
  resultTitle.textContent = output.title;
  answerList.replaceChildren(
    ...output.bullets.map((text) => {
      const li = document.createElement("li");
      li.innerHTML = text;
      return li;
    }),
  );

  // Lower panels
  switch (decision.route) {
    case "similarity_search":
      renderSimPanel(output);
      renderEvidenceSimilarity(output);
      break;
    case "rag_knowledge":
      dimSimPanel();
      renderEvidenceRag(output);
      break;
    case "weather_analytics":
      renderAnalyticsPanel(output);
      renderEvidenceAnalytics(output);
      break;
    case "data_retrieval":
      dimSimPanel();
      renderEvidenceData();
      break;
    default:
      dimSimPanel();
      renderEvidenceUnknown();
  }
}

// ─── Event listeners ──────────────────────────────────────────────────────────

document.querySelectorAll(".example-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    queryInput.value = chip.dataset.query;
    document.querySelectorAll(".example-chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    renderQuery(chip.dataset.query);
    document.querySelector("#router").scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
});

queryForm.addEventListener("submit", (event) => {
  event.preventDefault();
  document.querySelectorAll(".example-chip").forEach((c) => c.classList.remove("active"));
  renderQuery(queryInput.value);
});

document.querySelectorAll("nav a").forEach((link) => {
  link.addEventListener("click", () => {
    document.querySelectorAll("nav a").forEach((item) => item.classList.remove("active"));
    link.classList.add("active");
  });
});

// ─── Boot ─────────────────────────────────────────────────────────────────────

renderQuery(queryInput.value);