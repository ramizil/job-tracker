// Minimal native drag-and-drop Kanban: move a card to a lane => POST new status.
(function () {
  let dragged = null;

  document.querySelectorAll(".kcard").forEach((card) => {
    card.addEventListener("dragstart", () => {
      dragged = card;
      card.classList.add("dragging");
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("dragging");
      dragged = null;
    });
  });

  document.querySelectorAll(".lane-body").forEach((lane) => {
    lane.addEventListener("dragover", (e) => {
      e.preventDefault();
      lane.classList.add("drop");
    });
    lane.addEventListener("dragleave", () => lane.classList.remove("drop"));
    lane.addEventListener("drop", async (e) => {
      e.preventDefault();
      lane.classList.remove("drop");
      if (!dragged) return;
      const id = dragged.dataset.id;
      const status = lane.dataset.status;
      lane.appendChild(dragged);
      try {
        const res = await fetch(`/application/${id}/status`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status }),
        });
        if (!res.ok) throw new Error("status update failed");
        // refresh lane counts
        document.querySelectorAll(".lane").forEach((l) => {
          const c = l.querySelector(".count");
          if (c) c.textContent = l.querySelectorAll(".kcard").length;
        });
      } catch (err) {
        alert("Could not update status; reloading.");
        location.reload();
      }
    });
  });
})();
