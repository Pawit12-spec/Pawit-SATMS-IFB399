const MAX_TABLE_ROWS = 20;
const thermalConfig = window.THERMAL_CONFIG || {};
const eventsUrl = thermalConfig.eventsUrl;
const fileUrlTemplate = thermalConfig.imageUrlTemplate;

function makeImageUrl(filename) {
    if (!fileUrlTemplate || !filename) {
        return null;
    }

    return fileUrlTemplate.replace(
        "__FILENAME__",
        encodeURIComponent(filename)
    );
}

if (!eventsUrl) {
    console.error("Missing events URL; SSE will not start");
}

const es = eventsUrl ? new EventSource(eventsUrl) : null;

if (es) {
    es.onopen = () => {
        console.log("SSE connected");
    };

    es.onerror = () => {
        console.log("SSE error; readyState =", es.readyState);
    };
}

function formatTimestamp(value) {
    if (value === undefined || value === null || value === "") {
        return "";
    }

    if (typeof value === "number") {
        if (value > 1e15) {
            return new Date(value / 1e6).toISOString();
        }
        if (value > 1e12) {
            return new Date(value / 1e3).toISOString();
        }
        return new Date(value).toISOString();
    }

    const parsed = new Date(value);
    if (!Number.isNaN(parsed.getTime())) {
        return parsed.toISOString();
    }

    return value;
}

if (es) {
    es.addEventListener("thermal_image", (evt) => {
        const thermalImage = document.getElementById("thermal-image");
        const tableBody = document.getElementById("image-table");
        const data = JSON.parse(evt.data);

        console.log("thermal_image event received:", data);

        if (window.onThermalEvent) window.onThermalEvent(data);

        if (!data.filename) {
            return;
        }

        const imageUrl = makeImageUrl(data.filename);

        if (thermalImage && imageUrl) {
            thermalImage.src = imageUrl + "?t=" + Date.now();
        }

        if (tableBody && imageUrl) {
            const placeholderRow = tableBody.querySelector("td[colspan='2']");
            if (placeholderRow) {
                placeholderRow.parentElement.remove();
            }

            const timestampText = formatTimestamp(
                data.timestamp !== undefined ? data.timestamp : data.time
            );
            const newRow = document.createElement("tr");
            newRow.innerHTML = `
                <td>
                    <a href="${imageUrl}" target="_blank">
                        ${data.filename}
                    </a>
                </td>
                <td>${timestampText}</td>
            `;

            tableBody.prepend(newRow);

            if (tableBody.children.length > MAX_TABLE_ROWS) {
                tableBody.removeChild(tableBody.lastElementChild);
            }
        }
    });
}

initCsvExport('/api/export/thermal', 'thermal_images.csv');
