// Complement Token flow (Pre Userinfo Creation / Pre Access Token Creation) 用。
// RPアプリ(CMS/ArgoCD)はauthentik時代からgroups claimで権限を判定しているため、
// project roleのキーを配列のままgroups claimとして返す。関数名はaction名と一致必須。
function groupsClaim(ctx, api) {
  var grants = ctx.v1.user.grants;
  if (!grants || grants.count == 0) {
    return;
  }
  var groups = [];
  grants.grants.forEach(function (grant) {
    grant.roles.forEach(function (role) {
      if (groups.indexOf(role) < 0) {
        groups.push(role);
      }
    });
  });
  api.v1.claims.setClaim("groups", groups);
}
