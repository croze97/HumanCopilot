// The canonical CI-2 corpus is also read in place by Graphene capture/fuzz tests.
// This adapter only changes coordinate spaces; production lib.js does the crop.
import Gio from 'gi://Gio';
import {monitorPhysicalRect} from '../overlay/copilot-overlay@croze/lib.js';

export function checkCropVectors() {
    const file = Gio.File.new_for_uri(import.meta.url).get_parent()
        .get_child('vectors').get_child('crop-arithmetic.json');
    const [, bytes] = file.load_contents(null);
    const corpus = JSON.parse(new TextDecoder().decode(bytes));
    if (corpus.schema !== 'human-copilot.crop-arithmetic/v1' || !corpus.cases.length)
        throw new Error('missing or unsupported shared crop corpus');
    const ids = new Set();
    for (const vector of corpus.cases) {
        if (!vector.id || ids.has(vector.id))
            throw new Error('shared crop vector ids must be nonempty and unique');
        ids.add(vector.id);
        const [x, y, width, height] = vector.target;
        const [mx, my] = vector.monitor_origin;
        const scale = vector.scale[0] / vector.scale[1];
        const result = monitorPhysicalRect(
            {x: mx + x, y: my + y, width, height},
            {x: mx, y: my, width: vector.screen[0] / scale,
             height: vector.screen[1] / scale}, scale);
        const actual = result === null ? null
            : [result.x, result.y, result.width, result.height];
        if (JSON.stringify(actual) !== JSON.stringify(vector.expected))
            throw new Error(`shared crop vector ${vector.id}: expected ` +
                `${JSON.stringify(vector.expected)}, got ${JSON.stringify(actual)}`);
    }
    return corpus.cases.length;
}
