function probeBoardUpdates(){return fetch('/api/board-updates?version=0');}
function applyRemoteBoardUpdate(update){
 if(!update||typeof data==='undefined'||!data)return;
 const account=update.account;
 if(!account||account.id==null)return;
 const index=data.accounts.findIndex(item=>item.id===account.id);
 if(index>=0)data.accounts[index]=account;
 else data.accounts.push(account);
 if(update.record_version){
  const versions={...(data.record_versions||{})};
  versions['account:'+account.id]=update.record_version;
  data.record_versions=versions;
 }
}
