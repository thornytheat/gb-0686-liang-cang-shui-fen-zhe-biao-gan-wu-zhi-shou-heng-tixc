(function () {
  const feedback = document.querySelector('#feedback');
  const say = (text) => { if (feedback) feedback.textContent = text || ''; };

  async function send(url, body) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.message || '操作失败');
    return result;
  }

  const form = document.querySelector('#return-form');
  form?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const result = await send('/api/returns', {
        run_id: form.dataset.run,
        truck_id: form.elements.truck_id.value,
        net_kg: form.elements.net_kg.value,
        moisture: form.elements.moisture.value,
        impurity: form.elements.impurity.value,
        reason: form.elements.reason.value,
      });
      location.assign('/m/8?return=' + result.return_id);
    } catch (error) {
      say(error.message);
    } finally {
      button.disabled = false;
    }
  });

  const postForm = document.querySelector('#post-form');
  postForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = postForm.querySelector('button');
    button.disabled = true;
    try {
      await send('/api/returns/' + postForm.dataset.return + '/post', {});
      location.reload();
    } catch (error) {
      say(error.message);
      button.disabled = false;
    }
  });
})();
