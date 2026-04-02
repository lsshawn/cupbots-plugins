/// <reference path="../pb_data/types.d.ts" />

// mdpubs plugin — published notes tracking

migrate((app) => {
  const collection = new Collection({
    type: "base",
    name: "mdpubs_notes",
    listRule: "",
    viewRule: "",
    createRule: "",
    updateRule: "",
    deleteRule: null,
    fields: [
      {
        name: "key",
        type: "text",
        required: true,
      },
      {
        name: "company_id",
        type: "text",
      },
      {
        name: "note_id",
        type: "number",
        required: true,
      },
      {
        name: "title",
        type: "text",
      },
      {
        name: "url",
        type: "url",
      },
      {
        name: "tags",
        type: "json",
      },
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_mdpubs_key_company ON mdpubs_notes (key, company_id)",
    ],
  });
  app.save(collection);
}, (app) => {
  const collection = app.findCollectionByNameOrId("mdpubs_notes");
  app.delete(collection);
});
