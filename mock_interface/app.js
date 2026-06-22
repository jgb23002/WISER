const demos = {
  impact: {
    query: "impact of the new storm in Connecticut",
    route: "rag_knowledge",
    reason: "Matched documentation/RAG knowledge keywords.",
    keywords: "impact, storm",
    mode: "External context",
    title: "Storm impact summary",
    bullets: [
      "Retrieved NOAA/NHC/NCEI context for Connecticut storm-impact evidence. [1]",
      "Linked National Hurricane Center PDFs are extracted and chunked into RAG tables. [2]",
      "OpenAI summaries are optional; local extractive fallback keeps the workflow available. [1]",
    ],
  },
  rag: {
    query: "explain the storm similarity algorithm",
    route: "rag_knowledge",
    reason: "Matched documentation/RAG knowledge keywords.",
    keywords: "explain, algorithm",
    mode: "Local RAG",
    title: "Grounded knowledge answer",
    bullets: [
      "The RAG agent indexes project docs, scripts, SQL schema, and source files. [1]",
      "It retrieves the most relevant chunks and returns cited statements. [1]",
      "This lets WISER explain implementation details without manually searching files. [2]",
    ],
  },
  analytics: {
    query: "top counties by outage",
    route: "weather_analytics",
    reason: "Matched weather/outage analytics keywords.",
    keywords: "top, counties, outage",
    mode: "SQLite analytics",
    title: "Top outage counties",
    bullets: [
      "Fairfield ranks first with 43,475 total outages across 294 storms.",
      "Hartford ranks second with 23,307 total outages.",
      "Litchfield ranks third with 17,601 total outages.",
    ],
  },
  similarity: {
    query: "find storms similar to wind=9.2 precip=45 in Fairfield",
    route: "similarity_search",
    reason: "Matched similarity search keywords.",
    keywords: "similar, wind=, precip=",
    mode: "Z-score similarity",
    title: "Similar historical storms",
    bullets: [
      "The similarity engine standardizes each weather feature before ranking.",
      "Wind speed, precipitation, and wind-threshold duration features can be combined.",
      "The output gives comparable historical storms with observed outage counts.",
    ],
  },
  data: {
    query: "build sqlite database from csv",
    route: "data_retrieval",
    reason: "Matched data/database-oriented keywords.",
    keywords: "sqlite, database, csv",
    mode: "Data processing",
    title: "Database build workflow",
    bullets: [
      "The data retrieval agent normalizes the raw storm CSV into relational tables.",
      "It rebuilds Storm, County, and WeatherData records in SQLite.",
      "This prepares the database used by analytics and similarity searches.",
    ],
  },
};

const queryInput = document.querySelector("#query-input");
const routePill = document.querySelector("#route-pill");
const modePill = document.querySelector("#mode-pill");
const resultTitle = document.querySelector("#result-title");
const answerList = document.querySelector("#answer-list");
const queryForm = document.querySelector("#query-form");
const presets = document.querySelectorAll(".preset");
const routerQueryText = document.querySelector("#router-query-text");
const routerReason = document.querySelector("#router-reason");
const routerSelected = document.querySelector("#router-selected");
const routerKeywords = document.querySelector("#router-keywords");
const routeCards = document.querySelectorAll(".route-card");

function renderDemo(key) {
  const demo = demos[key] || demos.impact;
  queryInput.value = demo.query;
  routePill.textContent = demo.route;
  modePill.textContent = demo.mode;
  resultTitle.textContent = demo.title;
  routerQueryText.textContent = demo.query;
  routerReason.textContent = demo.reason;
  routerSelected.textContent = demo.route;
  routerKeywords.textContent = demo.keywords;
  answerList.replaceChildren(
    ...demo.bullets.map((text) => {
      const item = document.createElement("li");
      item.textContent = text;
      return item;
    }),
  );
  presets.forEach((button) => {
    button.classList.toggle("active", button.dataset.demo === key);
  });
  routeCards.forEach((card) => {
    card.classList.toggle("active", card.dataset.route === demo.route);
  });
}

function inferDemoFromQuery(query) {
  const text = query.toLowerCase();
  if (text.includes("top") || text.includes("outage")) {
    return "analytics";
  }
  if (
    text.includes("database") ||
    text.includes("sqlite") ||
    text.includes("csv") ||
    text.includes("normalize")
  ) {
    return "data";
  }
  if (text.includes("similar") && (text.includes("wind") || text.includes("precip"))) {
    return "similarity";
  }
  if (text.includes("explain") || text.includes("algorithm")) {
    return "rag";
  }
  return "impact";
}

presets.forEach((button) => {
  button.addEventListener("click", () => renderDemo(button.dataset.demo));
});

queryForm.addEventListener("submit", (event) => {
  event.preventDefault();
  renderDemo(inferDemoFromQuery(queryInput.value));
});

document.querySelectorAll("nav a").forEach((link) => {
  link.addEventListener("click", () => {
    document.querySelectorAll("nav a").forEach((item) => item.classList.remove("active"));
    link.classList.add("active");
  });
});
