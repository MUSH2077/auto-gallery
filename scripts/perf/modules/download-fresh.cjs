'use strict';
const base = require('./download.cjs');
const {python} = require('../bridge.cjs');
module.exports = {
  ...base, name: 'download-fresh', concurrency: [1], repeats: 1,
  async prepare(context) {
    const original = await base.prepare(context);
    const repositories = [];
    for (const repository of original.repositories) {
      const preview = await python(context, 'control.py', {action: 'preview', source: repository.source, url: repository.url});
      if (!preview.choices.length) throw Error('No unseen work available in the preview for ' + repository.source);
      const choice = preview.choices[0];
      const url = repository.source === 'pixiv' ? 'https://www.pixiv.net/artworks/' + choice.source_work_id :
        repository.source === 'x' ? 'https://x.com/Eul_bong/status/' + choice.source_work_id :
        'https://danbooru.donmai.us/posts?tags=' + encodeURIComponent('yokoyari_mengo id:' + choice.source_work_id);
      repositories.push({...repository, url, selected_work: choice});
    }
    return {...original, repositories, tasks: repositories.length, direct_requests: true,
      mode: 'public_manual_url_download_for_unseen_works_by_the_fixed_artists'};
  },
  async execute(context, prepared, options) {
    const result = await base.execute(context, prepared, options);
    if (result.status === 200 && !result.measurements?.snapshot?.media_files)
      return {...result, status: 409, measurements: {...result.measurements, invalid_fresh_sample: true}};
    return result;
  },
};
