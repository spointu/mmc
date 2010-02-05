<?

/*
 * (c) 2004-2007 Linbox / Free&ALter Soft, http://linbox.com
 * (c) 2007-2009 Mandriva, http://www.mandriva.com
 *
 * $Id$
 *
 * This file is part of Mandriva Management Console (MMC).
 *
 * MMC is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * MMC is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with MMC; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
 */

/* Get MMC includes */
require("../../../includes/config.inc.php");
require("../../../includes/i18n.inc.php");
require("../../../includes/acl.inc.php");
require("../../../includes/session.inc.php");
require("../../../includes/PageGenerator.php");
require("../includes/includes.php");
require('../includes/xmlrpc.inc.php');

$params = getParams();
$location = getCurrentLocation();
    
list($count, $masters) = xmlrpc_getLocationImages($location);

// forge params
#$nbItems = count($masters);
#$nbInfos = count($masters[0]);
$addAction = new ActionItem(_T("Add image to default boot menu", "imaging"), "master_add", "addbootmenu", "master", "imaging", "manage");
$emptyAction = new EmptyActionItem();
$addActions = array();

$a_label = array();
$a_desc = array();
$a_date = array();
$a_size = array();
$a_is_in_menu = array();

$i = -1;
foreach ($masters as $master) {
    $i += 1;
    #for($i=0;$i<$nbItems;$i++) {
    $list_params[$i] = $params;
    $list_params[$i]["itemid"] = $master['imaging_uuid'];
    $list_params[$i]["itemlabel"] = urlencode($master['desc']);
    
    if (!$master['image']) {
        $addActions[] = $addAction;
    } else {
        $addActions[] = $emptyAction;
    }

    $a_label[] = $master['desc'];
    $a_desc[] = $master['desc'];
    $a_date[] = _toDate($master['creation_date']);
    $a_size[] = $master['size'];
    $a_is_in_menu[] = ($master['image']?True:False);
}

$t = new TitleElement(_T("Available masters", "imaging"));
$t->display();

// show images list
$l = new ListInfos($a_label, _T("Label"));
$l->setParamInfo($list_params);
$l->addExtraInfo($a_desc, _T("Description", "imaging"));
$l->addExtraInfo($a_date, _T("Created", "imaging"));
$l->addExtraInfo($a_size, _T("Size (compressed)", "imaging"));
$l->addExtraInfo($a_is_in_menu, _T("In default boot menu", "imaging"));
$l->addActionItemArray($addActions);
$l->addActionItem(
    new ActionPopupItem(_T("Create bootable iso", "imaging"), 
    "master_iso", "backup", "master", "imaging", "manage")
);
$l->addActionItem(
    new ActionItem(_T("Edit image", "imaging"), 
    "master_edit", "edit", "master", "imaging", "manage")
);
$l->addActionItem(
    new ActionPopupItem(_T("Delete", "imaging"), 
    "master_delete", "delete", "master", "imaging", "manage")
);

$l->disableFirstColumnActionLink();
$l->display();

?>