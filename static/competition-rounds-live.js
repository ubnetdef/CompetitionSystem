(() => {
  const table = document.querySelector("[data-round-history]");
  if (!table) return;

  const body = table.querySelector("tbody");
  const statusElement = document.querySelector("[data-rounds-live-status]");
  const url = table.dataset.roundsUrl;
  const pollMilliseconds = Number(table.dataset.pollMs) || 5000;
  let timer = null;
  let requestInFlight = false;
  let failureLogged = false;

  function formatTimestamp(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return `${date.toISOString().replace("T", " ").slice(0, 19)} UTC`;
  }

  function formatDuration(startedAt, completedAt) {
    const start = Date.parse(startedAt);
    if (Number.isNaN(start)) return "—";
    const end = completedAt ? Date.parse(completedAt) : Date.now();
    if (Number.isNaN(end)) return "—";

    const elapsed = Math.max(0, Math.floor((end - start) / 1000));
    const seconds = elapsed % 60;
    const minutes = Math.floor(elapsed / 60) % 60;
    const hours = Math.floor(elapsed / 3600);
    return hours
      ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
      : `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  function updateElapsedTimes() {
    body.querySelectorAll("[data-round-duration]").forEach((cell) => {
      cell.textContent = formatDuration(
        cell.dataset.startedAt,
        cell.dataset.completedAt,
      );
    });
  }

  function createRoundRow(round) {
    const row = document.createElement("tr");

    const numberCell = document.createElement("td");
    numberCell.textContent = `#${round.number}`;

    const statusCell = document.createElement("td");
    const status = document.createElement("span");
    status.className = `pill pill-${round.status}`;
    status.textContent = round.status;
    statusCell.append(status);

    const startedCell = document.createElement("td");
    startedCell.textContent = formatTimestamp(round.started_at);

    const durationCell = document.createElement("td");
    durationCell.dataset.roundDuration = "";
    durationCell.dataset.startedAt = round.started_at || "";
    durationCell.dataset.completedAt = round.completed_at || "";

    const detailCell = document.createElement("td");
    detailCell.textContent = round.detail;

    row.append(numberCell, statusCell, startedCell, durationCell, detailCell);
    return row;
  }

  function schedulePoll() {
    window.clearTimeout(timer);
    timer = window.setTimeout(poll, pollMilliseconds);
  }

  async function poll() {
    if (requestInFlight) return;
    requestInFlight = true;

    try {
      const response = await fetch(url, {
        headers: { Accept: "application/json" },
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!response.ok) {
        throw new Error(`Round history update returned HTTP ${response.status}.`);
      }

      const snapshot = await response.json();
      if (!Array.isArray(snapshot.rounds)) {
        throw new Error("Round history update returned an invalid response.");
      }
      const rows = snapshot.rounds.map(createRoundRow);
      if (!rows.length) {
        const row = document.createElement("tr");
        const emptyCell = document.createElement("td");
        emptyCell.colSpan = 5;
        emptyCell.className = "table-empty";
        emptyCell.textContent = "No rounds have been scheduled.";
        row.append(emptyCell);
        rows.push(row);
      }
      body.replaceChildren(...rows);
      updateElapsedTimes();
      statusElement.textContent = `Live · updated ${new Date().toLocaleTimeString()}`;
      failureLogged = false;
    } catch (error) {
      statusElement.textContent = "Connection issue · retrying";
      if (!failureLogged) {
        console.error("Could not update recent scoring rounds.", error);
        failureLogged = true;
      }
    } finally {
      requestInFlight = false;
      schedulePoll();
    }
  }

  updateElapsedTimes();
  window.setInterval(updateElapsedTimes, 1000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      window.clearTimeout(timer);
      poll();
    }
  });
  poll();
})();
