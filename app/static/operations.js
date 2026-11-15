document.getElementById('window-server').addEventListener('change', event => {
  const serverId = Number(event.target.value);
  if (Number.isSafeInteger(serverId) && serverId > 0) {
    document.getElementById('window-form').action = '/servers/' + serverId + '/maintenance-window';
  }
});
