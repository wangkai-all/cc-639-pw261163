/* global Vue */
const { createApp } = Vue;

const TS_NAMES = {
  '1.2.840.10008.1.2': 'Implicit VR LE',
  '1.2.840.10008.1.2.1': 'Explicit VR LE',
  '1.2.840.10008.1.2.1.99': 'Deflated Explicit VR LE',
  '1.2.840.10008.1.2.2': 'Explicit VR BE',
};
const SOP_CLASS_NAMES = {
  '1.2.840.10008.5.1.4.1.1.2': 'CT Image Storage',
  '1.2.840.10008.5.1.4.1.1.4': 'MR Image Storage',
  '1.2.840.10008.5.1.4.1.1.7': 'Secondary Capture',
  '1.2.840.10008.5.1.4.1.1.88.59': 'Key Object Selection',
  '1.2.840.10008.5.1.4.1.1.88.11': 'Basic Text SR',
};
const STATUS_TEXT = {
  stored: '已入库',
  duplicate: '重复（内容一致）',
  conflict: '标识冲突',
  quarantined: '已隔离',
};
const STATUS_BADGE = {
  stored: 'ok',
  duplicate: 'neutral',
  conflict: 'bad',
  quarantined: 'warn',
};

async function api(path, options) {
  const rsp = await fetch(path, options);
  if (!rsp.ok) {
    let detail = rsp.statusText;
    try { detail = (await rsp.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return rsp.json();
}

// Recursive field-table component (sequences expand inline).
const FieldRows = {
  name: 'FieldRows',
  props: ['rows'],
  template: `
    <table class="fields-table">
      <tbody>
        <template v-for="(row, i) in rows" :key="i">
          <tr :class="{'private-row': row.private}">
            <td class="tag">{{ row.tag }}</td>
            <td class="kw">{{ row.keyword || '(私有)' }}</td>
            <td class="vr">{{ row.vr }}</td>
            <td class="val">{{ row.value }}</td>
          </tr>
          <tr v-if="row.children">
            <td></td><td colspan="3">
              <div v-for="child in row.children" :key="child.item" class="seq-item">
                <div class="muted">项 {{ child.item }}</div>
                <field-rows :rows="child.rows"></field-rows>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>`,
};

const FieldGroups = {
  name: 'FieldGroups',
  components: { FieldRows },
  props: ['rows'],
  computed: {
    groups() {
      const order = ['文件元信息', '标识与引用', '身份与描述', '采集参数与其他', '私有字段'];
      const byGroup = {};
      for (const row of this.rows || []) {
        (byGroup[row.group] = byGroup[row.group] || []).push(row);
      }
      return order.filter((g) => byGroup[g]).map((g) => ({ name: g, rows: byGroup[g] }));
    },
  },
  template: `
    <div>
      <details v-for="g in groups" :key="g.name" class="fields-group" open>
        <summary>{{ g.name }}（{{ g.rows.length }}）</summary>
        <field-rows :rows="g.rows"></field-rows>
      </details>
    </div>`,
};

createApp({
  components: { FieldGroups },
  data() {
    return {
      tab: 'query',
      remote: {
        loading: false, error: '', studies: [], study: null,
        series: [], seriesUid: null, instances: [], retrieving: null,
      },
      retrieveReport: null,
      retrieveResults: [],
      local: { hierarchy: [], selected: null, detail: null, refs: null },
      pixelError: false,
      conflicts: [],
      quarantine: [],
      reports: { receiving: [], integrity: null, rules: null },
      exporting: false,
    };
  },
  computed: {
    pixelUrl() {
      return this.local.selected ? `/api/instances/${this.local.selected}/pixel.png` : '';
    },
  },
  methods: {
    tsName(uid) { return TS_NAMES[uid] || uid || ''; },
    sopClassName(uid) { return SOP_CLASS_NAMES[uid] || uid || ''; },
    statusText(s) { return STATUS_TEXT[s] || s; },
    statusBadge(s) { return STATUS_BADGE[s] || 'neutral'; },
    shortUid(uid) { return uid ? `…${uid.slice(-12)}` : ''; },

    switchTab(tab) {
      this.tab = tab;
      if (tab === 'local') this.loadLocal();
      if (tab === 'reports') this.loadReports();
    },

    async queryStudies() {
      this.remote.loading = true;
      this.remote.error = '';
      this.remote.series = [];
      this.remote.instances = [];
      try {
        this.remote.studies = await api('/api/remote/studies');
      } catch (e) {
        this.remote.error = `查询失败：${e.message}`;
      } finally {
        this.remote.loading = false;
      }
    },
    async selectStudy(uid) {
      this.remote.study = uid;
      this.remote.seriesUid = null;
      this.remote.instances = [];
      this.remote.series = await api(`/api/remote/studies/${uid}/series`);
    },
    async selectSeries(se) {
      this.remote.seriesUid = se.SeriesInstanceUID;
      this.remote.instances = await api(
        `/api/remote/series/instances?study_uid=${se.StudyInstanceUID}&series_uid=${se.SeriesInstanceUID}`,
      );
    },
    async retrieveSeries(se) {
      this.remote.retrieving = se.SeriesInstanceUID;
      this.retrieveReport = null;
      this.retrieveResults = [];
      try {
        this.retrieveReport = await api('/api/retrieve', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ study_uid: se.StudyInstanceUID, series_uid: se.SeriesInstanceUID }),
        });
        const log = await api('/api/reports/receiving');
        const op = log.find((o) => o.id === this.retrieveReport.op_id);
        this.retrieveResults = op ? op.results : [];
      } catch (e) {
        this.remote.error = `接收失败：${e.message}`;
      } finally {
        this.remote.retrieving = null;
      }
    },

    async loadLocal() {
      this.local.hierarchy = await api('/api/studies');
      this.conflicts = await api('/api/conflicts');
      this.quarantine = await api('/api/quarantine');
    },
    async selectInstance(uid) {
      this.local.selected = uid;
      this.pixelError = false;
      this.local.detail = await api(`/api/instances/${uid}`);
      this.local.refs = await api(`/api/instances/${uid}/references`);
    },

    async loadReports() {
      this.reports.receiving = await api('/api/reports/receiving');
      this.reports.integrity = await api('/api/reports/integrity');
      this.reports.rules = await api('/api/anonymization/rules');
    },
    async exportZip() {
      this.exporting = true;
      try {
        const rsp = await fetch('/api/export', { method: 'POST' });
        if (!rsp.ok) throw new Error(rsp.statusText);
        const blob = await rsp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'dicom_export.zip';
        a.click();
        URL.revokeObjectURL(url);
      } finally {
        this.exporting = false;
      }
    },
    async resetStore() {
      if (!confirm('确定清空本地接收库？')) return;
      await api('/api/reset', { method: 'POST' });
      this.local = { hierarchy: [], selected: null, detail: null, refs: null };
      this.conflicts = [];
      this.quarantine = [];
    },
  },
}).mount('#app');
