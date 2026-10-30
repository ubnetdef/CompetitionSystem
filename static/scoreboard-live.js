(() => {
  const board = document.querySelector("[data-live-board]");
  if (!board) return;

  const statusElement = document.querySelector("[data-live-status]");
  const roundElement = document.querySelector("[data-live-round]");
  const table = board.querySelector(".scoreboard-table");
  const head = table.querySelector("thead");
  const body = board.querySelector(".scoreboard-table tbody");
  const url = board.dataset.liveUrl;
  const pollMilliseconds = Number(board.dataset.pollMs) || 5000;
  let etag = null;
  let timer = null;
  let requestInFlight = false;
  let failureLogged = false;

  function setConnectionState(message, connected) {
    statusElement.textContent = message;
    statusElement.classList.toggle("is-live", connected);
    statusElement.classList.toggle("is-retrying", !connected);
  }

  function renderBoard(snapshot) {
    const resultByCell = new Map(
      snapshot.results.map((result) => [
        `${result.team_id}:${result.service_id}`,
        result.passed,
      ]),
    );

    const headerRow = document.createElement("tr");
    const teamHeading = document.createElement("th");
    teamHeading.className = "sticky-team";
    teamHeading.textContent = "Team";
    headerRow.append(teamHeading);
    snapshot.services.forEach((service) => {
      const heading = document.createElement("th");
      heading.className = "score-check-heading";
      heading.dataset.serviceId = service.id;
      const group = document.createElement("span");
      group.textContent = service.group;
      const checkType = document.createElement("small");
      checkType.textContent = service.check_type;
      heading.append(group, checkType);
      headerRow.append(heading);
    });
    head.replaceChildren(headerRow);

    const rows = snapshot.teams.map((team) => {
      const row = document.createElement("tr");
      row.dataset.teamId = team.id;
      const teamCell = document.createElement("td");
      teamCell.className = "sticky-team team-board-cell";
      const name = document.createElement("strong");
      name.textContent = team.name;
      teamCell.append(name);
      row.append(teamCell);

      snapshot.services.forEach((service) => {
        const cell = document.createElement("td");
        cell.className = "score-result-cell";
        cell.dataset.teamId = team.id;
        cell.dataset.serviceId = service.id;
        const result = resultByCell.get(`${team.id}:${service.id}`);
        const value = service.paused
          ? "paused"
          : !service.enabled
            ? "unknown"
            : result === undefined
              ? "unknown"
              : result
                ? "pass"
                : "fail";
        const label =
          value === "pass"
            ? "Latest check passed"
            : value === "fail"
              ? "Latest check failed"
              : value === "paused"
                ? "Scoring is paused"
                : service.enabled
                  ? "No completed result yet"
                  : "Check is disabled";
        cell.dataset.resultStatus = value;
        cell.setAttribute("aria-label", label);
        cell.title = label;
        const status = document.createElement("span");
        status.className = "visually-hidden";
        status.textContent = value;
        cell.append(status);
        row.append(cell);
      });

      return row;
    });
    body.replaceChildren(...rows);

    roundElement.textContent = `Round ${snapshot.competition.round_number}`;
    setConnectionState(`Live · updated ${new Date().toLocaleTimeString()}`, true);
  }

  function schedulePoll() {
    window.clearTimeout(timer);
    timer = window.setTimeout(poll, pollMilliseconds);
  }

  async function poll() {
    if (requestInFlight) return;
    requestInFlight = true;
    const headers = { Accept: "application/json" };
    if (etag) headers["If-None-Match"] = etag;

    try {
      const response = await fetch(url, {
        headers,
        cache: "no-store",
        credentials: "same-origin",
      });
      if (response.status === 304) {
        setConnectionState(`Live · checked ${new Date().toLocaleTimeString()}`, true);
        failureLogged = false;
        return;
      }
      if (!response.ok) {
        throw new Error(`Scoreboard update returned HTTP ${response.status}.`);
      }
      const snapshot = await response.json();
      etag = response.headers.get("ETag") || etag;
      renderBoard(snapshot);
      failureLogged = false;
    } catch (error) {
      setConnectionState("Connection issue · retrying", false);
      if (!failureLogged) {
        console.error("Could not update the live scoreboard.", error);
        failureLogged = true;
      }
    } finally {
      requestInFlight = false;
      schedulePoll();
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      window.clearTimeout(timer);
      poll();
    }
  });
  poll();
})();
