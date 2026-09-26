import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

app.registerExtension({
    name: "Higgsfield.LocalStatus",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "HFVideoGenerate") return;
        const created = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            created?.apply(this, arguments);
            const model = this.widgets.find(w => w.name === "model");
            const resolution = this.widgets.find(w => w.name === "resolution");
            const duration = this.widgets.find(w => w.name === "duration");
            const format = this.widgets.find(w => w.name === "output_format");
            const update = () => {
                const oldModel = model.value.includes("2.0");
                resolution.options.values = oldModel ? ["720p", "480p", "1080p", "4k"] : ["720p", "480p"];
                duration.options.max = oldModel ? 15 : 30;
                format.options.values = oldModel ? ["mp4"] : ["mp4", "mov"];
            };
            const changed = model.callback;
            model.callback = function () {
                changed?.apply(this, arguments);
                update();
            };
            const configured = this.onConfigure;
            this.onConfigure = function () {
                configured?.apply(this, arguments);
                update();
            };
            update();
        };
    },
    setup() {
        api.addEventListener("higgsfield.status", ({ detail }) => {
            const node = app.graph.getNodeById(detail.node_id);
            if (!node) return;
            let status = node.widgets?.find(w => w.name === "Higgsfield status");
            if (!status) {
                status = node.addWidget("text", "Higgsfield status", "", () => {}, { serialize: false });
                status.options.serialize = false;
            }
            status.value = detail.status + (detail.request_id ? ` | ${detail.request_id}` : "");
            app.graph.setDirtyCanvas(true, false);
        });
    },
});
